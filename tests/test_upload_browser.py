"""Real service-worker transfers: interrupted multipart-free chunks, persisted file, closed tab and retry."""
import asyncio
import json
import random
import socket
import threading
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.responses import JSONResponse

from apk_factory import make_apk
from appshelf.android import uploads
from helpers import sign_in
from test_fourpda import local_chromium
from test_web_android import work


@pytest.fixture
def upload_server(make_web, cfg, conn, clock, acct):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    local_cfg = replace(cfg, public_base=base)
    web = make_web(local_cfg, conn)
    sign_in(web.client, local_cfg, conn, clock, acct.user_id)
    state = SimpleNamespace(hold=True, accepted=Counter(), requests=Counter(), lost_ack=False,
                            finish_lost_ack=False, finished=threading.Event(), ready=threading.Event())

    @web.app.middleware("http")
    async def controlled_transport(request, call_next):
        path = request.url.path
        if "/parts/" in path:
            index = int(path.rsplit("/", 1)[-1])
            state.requests[index] += 1
            if index >= 2:
                while state.hold:
                    await asyncio.sleep(0.01)
            response = await call_next(request)
            if response.status_code == 200:
                state.accepted[index] += 1
                if index == 2 and not state.lost_ack:
                    state.lost_ack = True
                    return JSONResponse({"error": "test reply lost after durable acceptance"}, status_code=503)
            return response
        response = await call_next(request)
        if path.endswith("/finish") and response.status_code == 200:
            state.finished.set()
            if state.finish_lost_ack:
                state.finish_lost_ack = False
                return JSONResponse({"error": "test finish response lost"}, status_code=503)
        return response

    original_lifespan = web.app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            state.ready.set()
            yield
    web.app.router.lifespan_context = lifespan

    server = uvicorn.Server(uvicorn.Config(web.app, log_level="critical", access_log=False, timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    assert state.ready.wait(5)
    try:
        yield SimpleNamespace(base=base, web=web, state=state, cfg=local_cfg)
    finally:
        state.hold = False
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


def browser_context(browser, server, width=390):
    context = browser.new_context(viewport={"width": width, "height": 844})
    context.add_cookies([{"name": cookie.name, "value": cookie.value, "url": server.base}
                         for cookie in server.web.client.cookies.jar])
    return context


@pytest.mark.integration
@pytest.mark.timeout(45)
@pytest.mark.parametrize("width,theme", [(320, "light"), (320, "dark"), (1280, "dark")])
def test_resumable_browser_pause_reload_and_retry(upload_server, conn, tmp_path, local_chromium, width, theme):
    server = upload_server
    apk = make_apk(tmp_path / "large.apk", files={"assets/padding": random.Random(42).randbytes(5 * 1024 * 1024)})
    assert apk.stat().st_size > uploads.CHUNK_SIZE * 5
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            context = browser_context(browser, server, width)
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.base + "/theme?set=" + theme)
            page.goto(server.base + "/android/add#add-file")  # сегмент «Файл»
            page.locator('form[data-apk-upload] input[type="file"]').set_input_files(apk)
            page.locator("form[data-apk-upload]").get_by_role("button", name="Загрузить", exact=True).click()
            page.wait_for_function("document.querySelector('form[data-apk-upload] progress').value > 0", timeout=10000)
            page.get_by_role("button", name="Пауза", exact=True).click()
            page.wait_for_function("document.querySelector('form[data-apk-upload] [data-upload-status]').textContent.includes('На паузе')")
            row = conn.execute("SELECT id,parts FROM android_uploads").fetchone()
            saved = json.loads(row["parts"])
            assert saved and all(index in ("0", "1") for index in saved)
            server.web.app.state.worker.recover()
            page.reload()
            region = page.get_by_role("region", name="Загрузки APK")
            assert "На паузе" in region.inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(server.cfg.data_dir / f"resumable-{width}-{theme}.png"), full_page=True)
            # Preserve the file's fingerprint but change a byte inside an already accepted part.
            page.evaluate("""async () => {
              const db = await new Promise((resolve, reject) => {
                const r = indexedDB.open('appshelf-apk-uploads'); r.onsuccess = () => resolve(r.result); r.onerror = reject;
              });
              const record = await new Promise(resolve => {
                const r = db.transaction('uploads').objectStore('uploads').getAll(); r.onsuccess = () => resolve(r.result[0]);
              });
              const bytes = new Uint8Array(await record.blob.arrayBuffer()); bytes[100000] ^= 1;
              record.blob = new Blob([bytes]);
              await new Promise((resolve, reject) => {
                const tx = db.transaction('uploads','readwrite'); tx.objectStore('uploads').put(record);
                tx.oncomplete = resolve; tx.onerror = reject;
              }); db.close();
            }""")
            region.get_by_role("button", name="Продолжить", exact=True).click()
            page.wait_for_function("document.querySelector('[aria-label=\"Загрузки APK\"] [data-upload-status]').textContent.includes('Файл отличается')")
            server.state.hold = False
            page.locator('form[data-apk-upload] input[type="file"]').set_input_files(apk)
            page.locator("form[data-apk-upload]").get_by_role("button", name="Загрузить", exact=True).click()
            assert server.state.finished.wait(12)
            page.wait_for_function("document.querySelector('[aria-label=\"Загрузки APK\"]') === null || document.querySelector('[aria-label=\"Загрузки APK\"]').hidden", timeout=10000)
            assert not errors
            assert server.state.lost_ack
            assert all(server.state.accepted[int(index)] == 1 for index in saved)
            assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 1
            work(server.web, conn)
            assert conn.execute("SELECT COUNT(*) FROM android_versions").fetchone()[0] == 1
        finally:
            server.state.hold = False
            browser.close()


@pytest.mark.integration
@pytest.mark.timeout(45)
def test_service_worker_upload_continues_without_open_page(upload_server, conn, tmp_path, local_chromium):
    server = upload_server
    apk = make_apk(tmp_path / "background.apk", files={"assets/padding": random.Random(43).randbytes(4 * 1024 * 1024)})
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            context = browser_context(browser, server)
            page = context.new_page()
            page.set_default_timeout(5000)
            page.goto(server.base + "/android/add#add-file")  # сегмент «Файл»
            page.locator('form[data-apk-upload] input[type="file"]').set_input_files(apk)
            page.locator("form[data-apk-upload]").get_by_role("button", name="Загрузить", exact=True).click()
            page.wait_for_function("document.querySelector('form[data-apk-upload] progress').value > 0", timeout=10000)
            assert page.evaluate("navigator.serviceWorker.controller !== null")
            assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 0
            page.get_by_role("button", name="Пауза", exact=True).click()
            page.locator("form[data-apk-upload]").get_by_role("button", name="Продолжить", exact=True).click()
            page.close()
            server.state.hold = False
            assert server.state.finished.wait(12)
            assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 1
            page = context.new_page()
            page.goto(server.base + "/android")
            page.wait_for_function("document.querySelector('[aria-label=\"Загрузки APK\"]') === null || document.querySelector('[aria-label=\"Загрузки APK\"]').hidden")
            work(server.web, conn)
            assert conn.execute("SELECT COUNT(*) FROM android_versions").fetchone()[0] == 1
        finally:
            server.state.hold = False
            browser.close()


@pytest.mark.integration
@pytest.mark.timeout(45)
def test_quota_fallback_reselects_file_without_reuploading_saved_parts(upload_server, conn, tmp_path, local_chromium):
    server = upload_server
    apk = make_apk(tmp_path / "quota.apk", files={"assets/padding": random.Random(44).randbytes(4 * 1024 * 1024)})
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            context = browser_context(browser, server)
            context.add_init_script("""Object.defineProperty(navigator, 'serviceWorker', {value:undefined});
              const put = IDBObjectStore.prototype.put;
              IDBObjectStore.prototype.put = function(value) {
                if (value.blob) throw new DOMException('forced file quota', 'QuotaExceededError');
                return put.apply(this, arguments);
              };""")
            page = context.new_page()
            page.set_default_timeout(5000)
            page.goto(server.base + "/android/add#add-file")  # сегмент «Файл»
            form = page.locator("form[data-apk-upload]")
            form.locator('input[type="file"]').set_input_files(apk)
            form.get_by_role("button", name="Загрузить", exact=True).click()
            page.wait_for_function("document.querySelector('form[data-apk-upload] progress').value > 0", timeout=10000)
            assert "Файл не сохранён в браузере" in form.inner_text()
            page.get_by_role("button", name="Пауза", exact=True).click()
            page.wait_for_function("document.querySelector('form[data-apk-upload] [data-upload-status]').textContent.includes('На паузе')")
            row = conn.execute("SELECT parts FROM android_uploads").fetchone()
            accepted = json.loads(row["parts"])
            page.reload()
            assert page.get_by_role("link", name="Выбрать тот же файл для продолжения").is_visible()
            renamed = tmp_path / "renamed.apk"
            renamed.write_bytes(apk.read_bytes())
            server.state.hold = False
            server.state.finish_lost_ack = True
            page.locator('form[data-apk-upload] input[type="file"]').set_input_files(renamed)
            page.locator("form[data-apk-upload]").get_by_role("button", name="Загрузить", exact=True).click()
            assert server.state.finished.wait(12)
            page.wait_for_function("document.querySelector('form[data-apk-upload] [data-upload-status]').textContent.includes('test finish response lost')")
            page.reload()
            page.wait_for_function("document.querySelector('[aria-label=\"Загрузки APK\"]') === null || document.querySelector('[aria-label=\"Загрузки APK\"]').hidden")
            assert all(server.state.accepted[int(index)] == 1 for index in accepted)
            assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 1
        finally:
            server.state.hold = False
            browser.close()
