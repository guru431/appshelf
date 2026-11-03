"""Real local Chromium checks for compact shelf interactions and accessible contrast."""
from urllib.parse import urlsplit

import pytest

from helpers import seed_app
from test_fourpda import local_chromium
from test_web_context import PKG, android_app
from appshelf.android import db, fetch, pin
from apk_factory import make_apk, png_bytes
from android_fakes import FakeHttp
from appshelf.android.sources import Sources
from test_web_android import work


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/android/add", "/android/public"])
@pytest.mark.parametrize("width", [320, 390, 1280])
def test_apk_upload_progress_and_retry_after_failure(web, conn, cfg, clock, tmp_path,
                                                    path, width, local_chromium):
    """Drive transport events deterministically while keeping the real form and browser UI."""
    public = db.ensure_public_shelf(conn, clock.iso())
    db.put_app(conn, public.id, PKG, "Shared app", "upload", "", clock.iso())
    apk = make_apk(tmp_path / "selected.apk")

    def route(target):
        url = urlsplit(target.request.url)
        response = web.client.get(url.path + ("?" + url.query if url.query else ""))
        target.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844})
            page.add_init_script("""window.uploadRequests = [];
              window.XMLHttpRequest = class extends EventTarget {
                constructor() { super(); this.upload = new EventTarget(); this.status = 0; }
                open(method, url) { this.method = method; this.url = url; }
                send(body) { this.body = body; window.uploadRequests.push(this); }
              };""")
            page.route("https://apps.example/**", route)
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                page.goto("https://apps.example" + path)
                if path == "/android/public":
                    page.get_by_label("Действия с Shared app", exact=True).click()
                    page.get_by_role("button", name="Сменить источник", exact=True).click()
                form = page.locator("form[data-apk-upload]")
                file = form.locator('input[type="file"]')
                file.set_input_files(apk)
                button = form.get_by_role("button", name="Загрузить", exact=True)
                button.click()
                status = form.get_by_role("status")
                progress = form.get_by_role("progressbar")
                assert progress.is_visible() and "0 %" in status.inner_text()
                assert file.is_disabled() and form.get_by_role("button", name="Загрузка…", exact=True).is_disabled()
                assert form.get_attribute("aria-busy") == "true"
                form.evaluate("f => f.dispatchEvent(new Event('submit', {cancelable:true}))")
                assert page.evaluate("uploadRequests.length") == 1
                assert page.evaluate("uploadRequests[0].method") == "POST"
                assert page.evaluate("uploadRequests[0].url").endswith(form.get_attribute("action"))
                assert page.evaluate("uploadRequests[0].body.get('file').name") == "selected.apk"
                page.evaluate("""() => uploadRequests[0].upload.dispatchEvent(new ProgressEvent('progress', {
                  lengthComputable:true, loaded:80*1048576, total:160*1048576}))""")
                assert progress.get_attribute("value") == "50"
                assert "50 % · 80 МБ из 160 МБ" in status.inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if path == "/android/add":
                    assert status.evaluate("e => e.getBoundingClientRect().bottom <= innerHeight - 72")
                if path == "/android/add":
                    page.screenshot(path=str(cfg.data_dir / f"upload-progress-{width}-{theme}.png"), full_page=True)
                page.evaluate("uploadRequests[0].upload.dispatchEvent(new ProgressEvent('load'))")
                assert "Ожидаем ответ сервера" in status.inner_text() and progress.get_attribute("value") == "100"
                assert form.get_by_role("button", name="Загрузка…", exact=True).is_disabled()
                page.evaluate("uploadRequests[0].status = 400; uploadRequests[0].dispatchEvent(new Event('load'))")
                assert "HTTP 400" in status.inner_text() and button.is_enabled() and file.is_enabled()
                assert form.get_attribute("aria-busy") is None
                assert file.evaluate("f => f.files[0].name") == "selected.apk"
                button.click()
                assert page.evaluate("uploadRequests.length") == 2
                page.evaluate("uploadRequests[1].dispatchEvent(new Event('error'))")
                assert "соединение прервалось" in status.inner_text() and button.is_enabled()
                if path == "/android/public":
                    page.screenshot(path=str(cfg.data_dir / f"upload-error-{width}-{theme}.png"), full_page=True)
                button.click()
                page.evaluate("""() => uploadRequests[2].upload.dispatchEvent(new ProgressEvent('progress', {
                  lengthComputable:false, loaded:31*1048576}))""")
                assert progress.get_attribute("value") is None and "передано 31 МБ" in status.inner_text()
                page.evaluate("uploadRequests[2].status = 413; uploadRequests[2].dispatchEvent(new Event('load'))")
                assert "до 1 ГБ" in status.inner_text() and button.is_enabled()
                button.click()
                page.evaluate("uploadRequests[3].status = 401; uploadRequests[3].dispatchEvent(new Event('load'))")
                assert "войдите снова" in status.inner_text() and button.is_enabled()
            assert conn.execute("SELECT COUNT(*) FROM android_jobs WHERE kind='upload'").fetchone()[0] == 0
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("prefix", ["/android", "/android/public"])
def test_android_upload_with_generic_file_type(web, conn, cfg, tmp_path, prefix, local_chromium):
    """Android file providers may report APKs as binary data or ZIP archives."""
    package = "ru.example.upload"

    def route(target):
        request = target.request
        url = urlsplit(request.url)
        result = web.client.request(request.method, url.path + ("?" + url.query if url.query else ""),
                                    content=request.post_data_buffer,
                                    headers={"user-agent": request.headers.get("user-agent", ""),
                                             "content-type": request.headers.get("content-type", "")},
                                    follow_redirects=request.resource_type == "xhr")
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844},
                                    user_agent="Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: errors.append(request.failure))
            page.route("https://apps.example/**", route)
            page.goto("https://apps.example" + prefix + "/add")
            for code, mime in ((1, "application/octet-stream"), (2, "application/zip")):
                if code == 2:
                    page.get_by_label("Действия с Upload app", exact=True).click()
                    page.get_by_role("button", name="Сменить источник", exact=True).click()
                form = page.locator('form[enctype="multipart/form-data"]')
                with page.expect_file_chooser() as opened:
                    form.locator('input[type="file"]').click()
                chooser = opened.value
                # A narrow accept filter makes APKs with a generic MIME type unselectable on Android.
                assert chooser.element.get_attribute("accept") in (None, "", "*/*")
                path = make_apk(tmp_path / f"upload-{code}.apk", package=package,
                                version_code=code, version_name=f"{code}.0", label="Upload app")
                chooser.set_files({"name": f"Приложение {code}.apk", "mimeType": mime, "buffer": path.read_bytes()})
                form.get_by_role("button", name="Загрузить", exact=True).click()
                page.wait_for_function("""prefix => location.pathname === prefix ||
                  document.querySelector('[data-upload-status].err')""", arg=prefix, timeout=10000)
                assert page.url == "https://apps.example" + prefix, (errors, form.inner_text())
                work(web, conn)
                page.reload()
                assert page.get_by_label("Действия с Upload app", exact=True).is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            shelf = db.public_shelf(conn) if prefix.endswith("/public") else db.shelf_for_user(conn, 1)
            view = db.list_apps(conn, shelf.id)[0]
            assert (view.current["version_code"], view.previous["version_code"]) == (2, 1)
            assert not list((cfg.tmp_dir / "web").glob("up-*.apk"))
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", [320, 390, 1280])
def test_shelf_layout_dialog_keyboard_and_primary_contrast(web, conn, cfg, clock, acct, width, local_chromium):
    playwright, executable = local_chromium
    shelf = android_app(conn, cfg, clock, acct.user_id)
    public = db.ensure_public_shelf(conn, clock.iso())
    db.put_app(conn, public.id, PKG, "Shared app", "upload", "", clock.iso())
    topic = "https://4pda.to/forum/index.php?showtopic=456"
    db.set_source(conn, shelf.id, PKG, "fourpda", "https://4pda.to/forum/dl/post/123/example.apk", topic)
    web.app.state.w.env.sources = Sources(FakeHttp(), pause=0)
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"))

    def route(target):
        request = target.request
        u = urlsplit(request.url)
        result = web.client.request(request.method, u.path + ("?" + u.query if u.query else ""),
                                    content=request.post_data_buffer,
                                    headers={"user-agent": request.headers.get("user-agent", "")}, follow_redirects=False)
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    user_agent=("Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile"
                                                if width < 700 else "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0"))
            page.context.route("https://apps.example/**", route)
            page.context.route("https://4pda.to/**", lambda target: target.fulfill(status=200, body="Source"))
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                for path in ("/?os=apple", "/history", "/blocked", "/android", "/android/add", "/android/public",
                             "/android/public/add", "/apple", "/admin", "/login", "/p"):
                    page.goto("https://apps.example" + path)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, theme, path)
                    heading = page.locator("header h1")
                    if heading.count():
                        assert heading.bounding_box()["x"] + heading.bounding_box()["width"] <= width
                page.goto("https://apps.example/android")
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_function("window.scrollY === 0")
                page.screenshot(path=str(cfg.data_dir / f"browser-{width}-{theme}.png"), full_page=True)
                page.get_by_label("Действия с Shared app", exact=True).click()
                menu = page.locator(".more[open] .action-list")
                page.wait_for_function("!document.querySelector('.more[open] .action-list').getAnimations().length")
                assert menu.evaluate("e => e.scrollWidth <= e.clientWidth")
                for label in ("Скачать из источника · 4PDA", "Тема на 4PDA"):
                    link = menu.get_by_role("link", name=label, exact=True)
                    assert link.is_visible() and link.bounding_box()["height"] >= 44
                    assert link.get_attribute("target") == "_blank"
                sync = menu.get_by_role("button", name="Добавить версии в публичную", exact=True)
                assert sync.is_visible() and sync.bounding_box()["height"] >= 44
                assert sync.locator("xpath=..").locator('input[name="sync_history"]').get_attribute("value") == "1"
                assert menu.get_by_role("link", name="Тема на 4PDA").get_attribute("href") == topic
                page.screenshot(path=str(cfg.data_dir / f"source-menu-{width}-{theme}.png"), full_page=True)
                with page.expect_popup() as opened:
                    menu.get_by_role("link", name="Скачать из источника · 4PDA", exact=True).click()
                popup = opened.value
                popup.wait_for_url("https://4pda.to/forum/dl/post/123/example.apk", timeout=5000)
                popup.close()
                assert page.url == "https://apps.example/android"
                page.get_by_role("button", name="Сменить источник", exact=True).first.click()
                dialog = page.get_by_role("dialog", name="Источник · Shared app")
                assert dialog.is_visible()
                assert dialog.evaluate("d => d.scrollWidth <= d.clientWidth && d.getBoundingClientRect().height <= innerHeight")
                page.get_by_label("Новый источник", exact=True).select_option("apkpure")
                assert page.get_by_text("Понимаю риск APKPure: без сверки", exact=False).is_visible()
                page.keyboard.press("Escape")
                assert not dialog.is_visible()
                page.wait_for_function("document.activeElement.matches('summary, [data-dialog]')")
                page.get_by_role("button", name="Сравнить версии и решить").click()
                assert page.get_by_role("dialog", name="Сравнить версии · Shared app").is_visible()
                assert page.get_by_role("dialog", name="Сравнить версии · Shared app").evaluate("d => d.scrollWidth <= d.clientWidth")
                page.keyboard.press("Escape")
                primary = page.get_by_role("link", name="Скачать APK", exact=True)
                for state in ("normal", "hover", "focus", "active"):
                    if state == "hover":
                        primary.hover()
                    elif state == "focus":
                        primary.focus()
                    elif state == "active":
                        primary.hover()
                        page.mouse.down()
                    page.wait_for_timeout(150)
                    ratio = primary.evaluate("""e => {
                      const s = getComputedStyle(e), rgb = x => x.match(/[\\d.]+/g).slice(0,3).map(Number);
                      let bg = rgb(s.backgroundColor), fg = rgb(s.color);
                      const b = s.filter.match(/brightness\\(([\\d.]+)\\)/); if(b) { bg = bg.map(x=>Math.min(255,x*Number(b[1]))); fg=fg.map(x=>Math.min(255,x*Number(b[1]))); }
                      const lum = v => v.map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0);
                      return (Math.max(lum(bg),lum(fg))+.05)/(Math.min(lum(bg),lum(fg))+.05);
                    }""")
                    assert ratio >= 4.5, (theme, state, ratio)
                    if state == "active":
                        page.mouse.move(0, 0)
                        page.mouse.up()
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("path", ["/android", "/android/public", "/p"])
def test_android_version_picker_updates_download_details_and_qr(web, make_web, conn, cfg, clock, acct,
                                                               width, path, local_chromium):
    pkg = "ru.example.history"
    shelves = (db.ensure_shelf(conn, acct.user_id, clock.iso()), db.ensure_public_shelf(conn, clock.iso()))
    for shelf in shelves:
        db.put_app(conn, shelf.id, pkg, "History app", "link", "https://example.org/history.apk", clock.iso())
        root = fetch.shelf_root(cfg, shelf)
        for code, signer in ((3, "a" * 64), (2, "a" * 64), (1, "b" * 64)):
            folder = root / f"{pkg}-{code}"
            folder.mkdir(parents=True)
            (folder / "icon.png").write_bytes(png_bytes(40))
            db.add_version(conn, root, shelf.id, pkg, db.NewApk(f"{code}.0", code, 23 if code == 1 else 28,
                           22 if code == 1 else 35, "", signer, "link", 10485760, str(code) * 64,
                           folder.name, True), clock.iso())
        assert db.accept_pending(conn, root, shelf.id, pkg)
    pin.set_pin(conn, "482913", clock.iso())
    guest = make_web(cfg, conn)
    guest.client.post("/p", data={"pin": "482913"})
    active = guest.client if path == "/p" else web.client

    def route(target):
        request = target.request
        u = urlsplit(request.url)
        if u.path.startswith("/d/") and u.path.endswith("/icon.png"):
            target.fulfill(status=200, content_type="image/png", body=png_bytes(40))
            return
        result = active.request(request.method, u.path + ("?" + u.query if u.query else ""),
                                content=request.post_data_buffer,
                                headers={"user-agent": request.headers.get("user-agent", "")})
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    user_agent=("Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile"
                                                if width < 700 else "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0"))
            errors = []
            page.on("pageerror", lambda error: errors.append(error))
            page.route("https://apps.example/**", route)
            for theme in ("light", "dark"):
                active.get(f"/theme?set={theme}")
                page.goto("https://apps.example" + path)
                select = page.get_by_label("Версия History app", exact=True)
                assert "3.0" in select.locator("option:checked").inner_text()
                choices = select.locator("option").evaluate_all("options => options.map(o => o.value)")
                assert len(choices) == 3
                select.select_option(choices[-1])
                card = select.locator("xpath=ancestor::div[contains(@class, 'card')]")
                assert card.get_by_role("link", name="Скачать APK", exact=True).get_attribute("href") == choices[-1]
                assert "1.0 · Android 6+" in card.locator("[data-apk-meta]").inner_text()
                warning = card.locator("[data-apk-warning]")
                assert warning.is_visible() and "Другая подпись" in warning.inner_text()
                assert "Android 15+" in warning.inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert select.bounding_box()["height"] >= 44
                if width >= 700 or path == "/android/public":
                    card.get_by_role("button", name="Показать QR-код для History app").click()
                    assert "?version=" in card.locator(".app-qr a").get_attribute("href")
                    assert "?version=" in card.locator(".app-qr img").get_attribute("src")
                if path == "/android":
                    assert "?version=" in card.locator(".more [data-copy]").get_attribute("data-copy")
                    page.screenshot(path=str(cfg.data_dir / f"versions-{width}-{theme}.png"), full_page=True)
                page.reload()
                assert "3.0" in page.get_by_label("Версия History app", exact=True).locator("option:checked").inner_text()
            assert not errors
        finally:
            browser.close()
