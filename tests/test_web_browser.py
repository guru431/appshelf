"""Real local Chromium checks for compact shelf interactions and accessible contrast."""
from pathlib import Path
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
from appshelf import people
from helpers import sign_in
from test_version_removal import saved_versions


@pytest.mark.integration
@pytest.mark.parametrize("width", [320, 1280])
def test_old_version_removal_confirmation_and_cancel(web, conn, cfg, saved_versions, width, local_chromium):
    from pathlib import Path
    s = saved_versions
    path = "/" if s.platform == "apple" else "/android/public" if s.platform == "public" else "/android"
    conn.execute(f"UPDATE {s.table} SET pinned=1 WHERE id=?", (s.old["id"],))

    def route(target):
        req, url = target.request, urlsplit(target.request.url)
        if url.path.startswith("/d/"):
            target.fulfill(status=200, content_type="image/png", body=png_bytes(40))
            return
        response = web.client.request(req.method, url.path + ("?" + url.query if url.query else ""),
                                      content=req.post_data_buffer,
                                      headers={"content-type": req.headers.get("content-type", "")},
                                      follow_redirects=False)
        target.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://apps.example/**", route)
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                page.goto("https://apps.example" + path)
                page.locator(".more > summary").first.click()
                page.get_by_role("button", name="Версии и закрепления", exact=True).click()
                dialog = page.locator("dialog[open]")
                assert dialog.get_by_role("link", name="Удалить 2.0", exact=True).count() == 0
                delete = dialog.get_by_role("link", name="Удалить 1.0", exact=True)
                assert delete.is_visible()
                assert dialog.locator(".version-row > .body").first.evaluate(
                    "e => e.clientHeight < 48 && e.clientWidth > 150")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                screenshots = Path(__file__).resolve().parents[1] / ".work"
                screenshots.mkdir(exist_ok=True)
                page.screenshot(path=str(screenshots / f"version-removal-{s.platform}-{width}-{theme}.png"))
                delete.click()
                page.wait_for_url(lambda url: urlsplit(url).path.endswith("/remove"))
                assert "Закрепление будет снято" in page.locator("main").inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if theme == "light":
                    page.get_by_role("link", name="Отмена", exact=True).click()
                    page.wait_for_url(lambda url: "/remove" not in url)
                    assert (s.root / s.old["dir"]).is_dir()
                else:
                    page.get_by_role("button", name="Удалить версию", exact=True).click()
                    page.wait_for_url(lambda url: "/remove" not in url)
                    assert not (s.root / s.old["dir"]).exists()
                    assert (s.root / s.current["dir"]).is_dir()
            assert not errors
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", [320, 390, 1280])
def test_short_screen_menu_settings_and_device_profile(web, conn, cfg, clock, tmp_path, width, local_chromium):
    from appshelf.android.fdroid_index import Build
    from appshelf.android.search import Option
    package = 'org.example.device'
    web.sources.found = [Option('fdroid', package, package, 'Device choice', '9', builds=(
        Build('9', 9, 'nine.apk', 'a' * 64, '', ('arm64-v8a',), 36, 35),
        Build('8', 8, 'eight.apk', 'b' * 64, '', ('x86',), 26, 35),
        Build('7', 7, 'seven.apk', 'c' * 64, '', ('arm64-v8a',), 26, 35)))]
    for code, minimum, target, abis, cert in [(1, 35, 22, ('x86',), b'CERT-A'),
                                             (2, 26, 35, ('arm64-v8a',), b'CERT-A'),
                                             (3, 26, 35, ('arm64-v8a',), b'CERT-B')]:
        path = make_apk(tmp_path / f'profile-{code}.apk', label='Device app', version_code=code,
                        version_name=f'{code}.0', min_sdk=minimum, target_sdk=target, abis=abis, cert=cert)
        web.client.post('/android/add/upload', files={'file': ('app.apk', path.read_bytes())})
        work(web, conn)
    for n in range(8):
        uid = people.create_user(conn, f'Участник с длинным именем {n}', 'member', None, clock.iso())
        sign_in(web.client, cfg, conn, clock, uid)
    web.client.get('/who/1?back=/android')

    def route(target):
        req, url = target.request, urlsplit(target.request.url)
        if url.path.startswith('/d/'):
            target.fulfill(status=200, content_type='image/png', body=png_bytes(40))
            return
        result = web.client.request(req.method, url.path + ('?' + url.query if url.query else ''),
                                    content=req.post_data_buffer,
                                    headers={'content-type': req.headers.get('content-type', '')}, follow_redirects=False)
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=['--disable-gpu'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': 568})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('https://apps.example/**', route)
            for theme in ('light', 'dark'):
                web.client.get(f'/theme?set={theme}')
                page.goto('https://apps.example/android')
                page.get_by_label('Выбрать полку', exact=True).click()
                sheet = page.locator('.acct .sheet')
                page.wait_for_function("document.querySelector('.acct .sheet').style.getPropertyValue('--sheet-top') !== ''")
                assert sheet.evaluate('e => e.scrollHeight > e.clientHeight')
                last = sheet.get_by_role('link', name='Публичная', exact=True)
                last.focus()
                assert last.evaluate('e => e.getBoundingClientRect().bottom < innerHeight - 60')
                page.keyboard.press('Enter')
                page.wait_for_url('**/android/public')
                assert page.locator('.tabs [aria-current="page"]').inner_text() == 'Каталог'
                page.get_by_role('link', name='Настройки', exact=True).click()
                page.wait_for_url('**/settings')
                assert page.get_by_role('navigation', name='Оформление').locator('[aria-current="true"]').inner_text() == ('Светлая' if theme == 'light' else 'Тёмная')
                assert page.get_by_role('link', name='Аккаунты Apple ID и Google').is_visible()
                # в «Настройках» форма открыта под своим заголовком, без раскрывашки
                assert page.get_by_role('heading', name='Android-устройство').is_visible()
                page.get_by_label('Android устройства').select_option('35')
                page.get_by_label('Архитектура устройства').select_option('arm64-v8a')
                page.get_by_role('button', name='Сохранить профиль', exact=True).click()
                page.goto('https://apps.example/android')
                # на Android-странице профиль — одна строка, форма в раскрывашке
                assert page.locator('[data-device-summary]').inner_text() == 'Устройство: Android 15 · ARM64 (только 64 бит)'
                assert page.locator('[data-device-form]').is_hidden()
                box = page.locator('[data-compatibility]')
                assert box.locator('summary').inner_text() == 'Совместима по известным требованиям'
                select = page.locator('[data-apk-select]')
                select.select_option(index=1)
                assert box.locator('summary').inner_text() == 'Есть ограничения'
                box.locator('summary').click()
                assert 'targetSdk 22' in box.inner_text() and 'APK: x86' in box.inner_text()
                page.reload()
                assert box.locator('summary').inner_text() == 'Совместима по известным требованиям'
                assert page.evaluate("JSON.parse(localStorage.getItem('appshelf-device-profile')).sdk") == 35
                select.select_option(index=1)
                assert page.locator('[data-apk-warning]').is_visible()
                for tag in page.locator('.app .tag, .app [data-apk-warning], .app [data-compatibility] summary').all():
                    ratio = tag.evaluate('''e => {
                      const rgba = x => x.match(/[\\d.]+/g).map(Number);
                      let chain = [], el = e;
                      while(el) { chain.unshift(rgba(getComputedStyle(el).backgroundColor)); el=el.parentElement; }
                      let bg=[255,255,255];
                      for(const c of chain) { const a=c.length>3?c[3]:1; bg=bg.map((x,i)=>c[i]*a+x*(1-a)); }
                      const fg=rgba(getComputedStyle(e).color);
                      const lum=c=>c.slice(0,3).map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0);
                      return (Math.max(lum(bg),lum(fg))+.05)/(Math.min(lum(bg),lum(fg))+.05);
                    }''')
                    assert ratio >= 4.5, (theme, ratio)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(cfg.data_dir / f'settings-profile-{width}-{theme}.png'), full_page=True)
                page.goto('https://apps.example/android/add?q=device')
                builds = page.locator('[data-fdroid-builds]')
                page.wait_for_selector('[data-fdroid-builds]', timeout=5000)
                page.wait_for_function("document.querySelector('[data-fdroid-builds]')?.value.endsWith('@7')", timeout=5000)
                builds.select_option(package + '@8')
                assert builds.input_value() == package + '@8'
                assert 'Есть ограничения' in page.locator('[data-build-hint]').inner_text()
                page.locator('[data-device-profile] > summary').click()
                page.get_by_label('Android устройства').select_option('36')
                page.get_by_role('button', name='Сохранить профиль', exact=True).click()
                assert builds.input_value() == package + '@8'  # Manual selection survives profile changes.
                page.get_by_role('button', name='Сбросить', exact=True).click()
                assert page.evaluate("localStorage.getItem('appshelf-device-profile')") is None
                page.goto('https://apps.example/android/public/add/from-shelf?person=1&package=ru.example.app')
                assert page.get_by_role('button', name='Добавить версии: 2').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
        finally:
            browser.close()


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
            page.add_init_script("""Object.defineProperty(window, 'indexedDB', {value:undefined});
              window.uploadRequests = [];
              window.XMLHttpRequest = class extends EventTarget {
                constructor() { super(); this.upload = new EventTarget(); this.status = 0; this.responseText = ''; }
                getResponseHeader(name) { return name.toLowerCase() === 'content-type' && this.contentType || null; }
                open(method, url) { this.method = method; this.url = url; }
                send(body) { this.body = body; window.uploadRequests.push(this); }
              };""")
            page.route("https://apps.example/**", route)
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                page.goto("https://apps.example" + path)
                if path == "/android/add":
                    page.get_by_role("navigation", name="Способ добавления").get_by_role("link", name="Файл").click()
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
                button.click()  # отказ проверки полки — text/plain: показать его текст, а не код
                page.evaluate("""() => Object.assign(uploadRequests[4], {status: 403, contentType: 'text/plain; charset=utf-8',
                  responseText: 'Нет доступа к указанной полке. Обновите страницу.'}).dispatchEvent(new Event('load'))""")
                assert "Нет доступа к указанной полке" in status.inner_text() and "HTTP" not in status.inner_text()
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
                                             "content-type": request.headers.get("content-type", ""),
                                             "x-chunk-sha256": request.headers.get("x-chunk-sha256", "")},
                                    follow_redirects=request.resource_type == "xhr")
        if result.status_code >= 400:
            errors.append((request.method, url.path, result.status_code, result.text[:500]))
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844},
                                    user_agent="Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile")
            page.add_init_script("Object.defineProperty(navigator, 'serviceWorker', {value:undefined});")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: errors.append(request.failure))
            page.route("https://apps.example/**", route)
            page.goto("https://apps.example" + prefix + "/add#add-file")
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
                with page.expect_navigation(url="https://apps.example" + prefix, wait_until="domcontentloaded", timeout=10000):
                    form.get_by_role("button", name="Загрузить", exact=True).click()
                assert page.url == "https://apps.example" + prefix, errors
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
def test_pending_uploads_tray_link_and_plain_errors(web, conn, cfg, clock, acct, tmp_path, local_chromium):
    """«Добавить» lists server uploads missing from this browser; the tray keeps its nodes and links to the card."""
    from appshelf.android import uploads
    from helpers import make_account
    android_app(conn, cfg, clock, acct.user_id)
    member = make_account(conn, clock, email="member@example.org", role="member", legacy=False, name="Мария")
    left = uploads.Uploads(cfg).create(conn, member.user_id, db.ensure_shelf(conn, member.user_id, clock.iso()).id,
                                       {"client_id": "c" * 32, "size": 5, "fingerprint": "0" * 64,
                                        "filename": "member.apk"}, clock.iso())
    refuse = []

    def route(target):
        request, url = target.request, urlsplit(target.request.url)
        if refuse and request.method == "POST" and url.path == "/android/uploads":
            target.fulfill(status=403, content_type="text/plain; charset=utf-8",
                           body="Нет доступа к указанной полке. Обновите страницу.")
            return
        result = web.client.request(request.method, url.path + ("?" + url.query if url.query else ""),
                                    content=request.post_data_buffer,
                                    headers={"content-type": request.headers.get("content-type", ""),
                                             "x-chunk-sha256": request.headers.get("x-chunk-sha256", "")},
                                    follow_redirects=False)
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script("Object.defineProperty(navigator, 'serviceWorker', {value:undefined});")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://apps.example/**", route)
            page.goto("https://apps.example/android/add")
            pending = page.locator("[data-pending-uploads]")
            cancel = pending.get_by_role("button", name="Отменить загрузку member.apk", exact=True)
            cancel.wait_for()
            assert "Мария" in pending.inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            cancel.click()
            pending.wait_for(state="hidden")
            assert not conn.execute("SELECT 1 FROM android_uploads WHERE id=?", (left["id"],)).fetchone()

            refuse.append(True)
            page.get_by_role("navigation", name="Способ добавления").get_by_role("link", name="Файл").click()
            form = page.locator("form[data-apk-upload]")
            form.locator('input[type="file"]').set_input_files(make_apk(tmp_path / "refused.apk"))
            form.get_by_role("button", name="Загрузить", exact=True).click()
            status = form.get_by_role("status")
            page.wait_for_function("s => s.textContent.includes('Нет доступа')", arg=status.element_handle())
            assert "HTTP" not in status.inner_text()
            refuse.clear()
            form.get_by_role("button", name="Отменить", exact=True).click()  # без номера загрузки — по client_id
            form.locator("[data-upload-feedback]").wait_for(state="hidden")

            page.evaluate("""() => new Promise((resolve, reject) => {
              const r = indexedDB.open('appshelf-apk-uploads', 1);
              r.onsuccess = () => {
                const tx = r.result.transaction('uploads', 'readwrite');
                tx.objectStore('uploads').put({key: 'card', clientId: 'd'.repeat(32), owner: %d,
                  endpoint: '/android/uploads?person=%d', package: '%s', target: '/android', size: 10,
                  fingerprint: '1'.repeat(64), filename: 'card.apk', name: '', loaded: 0, state: 'paused',
                  message: 'На паузе', updated: Date.now(), blob: null});
                tx.oncomplete = resolve; tx.onerror = reject;
              };
              r.onerror = reject;
            })""" % (acct.user_id, acct.user_id, PKG))
            page.reload()
            tray = page.get_by_role("region", name="Загрузки APK")
            link = tray.get_by_role("link", name="Выбрать тот же файл для продолжения", exact=True)
            assert link.get_attribute("href") == f"/android?person={acct.user_id}#source-{PKG}"
            page.evaluate("window.trayButton = document.querySelector('[aria-label=\"Загрузки APK\"] [data-upload-cancel]')")
            page.evaluate("dispatchEvent(new Event('online'))")
            page.wait_for_timeout(500)
            assert page.evaluate("window.trayButton.isConnected")
            link.click()
            page.wait_for_url(f"**/android?person={acct.user_id}#source-{PKG}")
            assert page.locator(f'dialog[id="source-{PKG}"][open]').is_visible()
            assert not errors
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
                sync = menu.get_by_role("link", name="Сравнить и добавить версии", exact=True)
                assert sync.is_visible() and sync.bounding_box()["height"] >= 44
                assert "/android/public/add/from-shelf" in sync.get_attribute("href")
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


def _route_to(client):
    """Запросы страницы — в TestClient; файлы /d/ — заглушка (иконка или APK)."""
    def route(target):
        request, u = target.request, urlsplit(target.request.url)
        if u.path.startswith("/d/"):
            target.fulfill(status=200, content_type="image/png", body=png_bytes(40))
            return
        result = client.request(request.method, u.path + ("?" + u.query if u.query else ""),
                                content=request.post_data_buffer,
                                headers={"content-type": request.headers.get("content-type", "")},
                                follow_redirects=False)
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)
    return route


@pytest.mark.integration
def test_public_search_explains_skipped_fourpda_topic(web, local_chromium):
    from urllib.parse import quote
    web.app.state.w.env.sources = Sources(FakeHttp(), sleep=lambda _: None, pause=0)
    topic = "https://4pda.to/forum/index.php?showtopic=456"
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://apps.example/**", _route_to(web.client))
            page.goto("https://apps.example/android/public/add?q=" + quote(topic, safe=""))
            page.wait_for_function("document.getElementById('search-summary').textContent.startsWith('Поиск завершён')")
            assert "4PDA: доступен только на личной полке" in page.locator("#source-statuses").inner_text()
            assert not errors
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/android", "/p"])
def test_download_mark_remembers_version_in_this_browser(web, make_web, conn, cfg, clock, acct, path, local_chromium):
    pkg = "ru.example.mark"
    shelf = (db.ensure_shelf(conn, acct.user_id, clock.iso()) if path == "/android"
             else db.ensure_public_shelf(conn, clock.iso()))
    db.put_app(conn, shelf.id, pkg, "Mark app", "link", "https://example.org/mark.apk", clock.iso())
    root = fetch.shelf_root(cfg, shelf)
    for code in (1, 2):
        (root / f"{pkg}-{code}").mkdir(parents=True)
        db.add_version(conn, root, shelf.id, pkg, db.NewApk(f"{code}.0", code, 28, 35, "", "a" * 64, "link", 1,
                       str(code) * 64, f"{pkg}-{code}", True), clock.iso())
    other = "ru.example.other"   # не скачивали — фильтр «Есть обновление для меня» её прячет
    db.put_app(conn, shelf.id, other, "Other app", "link", "https://example.org/other.apk", clock.iso())
    (root / f"{other}-1").mkdir(parents=True)
    db.add_version(conn, root, shelf.id, other, db.NewApk("1.0", 1, 28, 35, "", "a" * 64, "link", 1, "9" * 64,
                   f"{other}-1", True), clock.iso())
    client = web.client
    if path == "/p":
        pin.set_pin(conn, "482913", clock.iso())
        client = make_web(cfg, conn).client
        client.post("/p", data={"pin": "482913"})
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844}, accept_downloads=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://apps.example/**", _route_to(client))
            page.goto("https://apps.example" + path)
            card = page.locator(f'.app[data-package="{pkg}"]')
            mark = card.locator("[data-downloaded]")
            mine = page.get_by_label("Есть обновление для меня", exact=False)
            assert mark.is_hidden() and page.locator(".mine-filter").is_hidden()
            page.get_by_label("Версия Mark app", exact=True).select_option(index=1)
            with page.expect_download():
                card.get_by_role("link", name="Скачать APK", exact=True).click()
            assert mark.inner_text() == "Вы скачивали 1.0 — на полке 2.0"
            page.reload()
            assert mark.inner_text() == "Вы скачивали 1.0 — на полке 2.0"
            # фильтр только в этом браузере: оставляет карточки, где на полке новее скачанного
            assert page.locator(".mine-filter").inner_text() == "Есть обновление для меня · 1"
            mine.check()
            assert card.is_visible() and page.locator(f'.app[data-package="{other}"]').is_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            with page.expect_download():
                card.get_by_role("link", name="Скачать APK", exact=True).click()
            assert mark.inner_text() == "Скачана в этом браузере"
            # обновлять больше нечего — фильтр снят и спрятан, видны все карточки
            assert page.locator(".mine-filter").is_hidden() and page.locator(f'.app[data-package="{other}"]').is_visible()
            assert not errors
        finally:
            browser.close()


# Контраст текста с фоном под ним (полупрозрачные слои складываются до белого) — WCAG AA, 4.5:1.
CONTRAST = '''e => {
  const rgba = x => x.match(/[\\d.]+/g).map(Number);
  let chain = [], el = e;
  while(el) { chain.unshift(rgba(getComputedStyle(el).backgroundColor)); el=el.parentElement; }
  let bg=[255,255,255];
  for(const c of chain) { const a=c.length>3?c[3]:1; bg=bg.map((x,i)=>c[i]*a+x*(1-a)); }
  const fg=rgba(getComputedStyle(e).color);
  const lum=c=>c.slice(0,3).map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0);
  return (Math.max(lum(bg),lum(fg))+.05)/(Math.min(lum(bg),lum(fg))+.05);
}'''
NEW_TEXT = (".tip li, .tip .meta, .tip h2, .sortbar a, .tag, .add-seg a, .field-hint, [data-device-summary], .help p, "
            ".help h3, .help-toggle, .mine-filter, .row .body .meta, .file-pick .btn")


@pytest.mark.integration
@pytest.mark.parametrize("width", [390, 1280])
def test_help_tip_segments_and_removed_filter(web, conn, cfg, clock, acct, width, local_chromium, monkeypatch, tmp_path):
    """Подсказка новичку закрывается навсегда, «?» открывает «Как установить», «Добавить» помнит раздел,
    «Удалённые» фильтруются; новые элементы читаемы в обеих темах и не дают горизонтальной прокрутки."""
    from appshelf import removed, store as apple_store
    monkeypatch.setattr(removed, "load", lambda: [{"id": 123, "name": "Test App"}, {"id": 7, "name": "ВТБ Онлайн"},
                                                  {"id": 8, "name": "Альфа-Банк"}])
    seed_app(conn, cfg, clock, name="Test App", versions=("1.0", "2.0"))
    apple_store.mark_no_license(conn, 1, 7, clock.iso())
    android_app(conn, cfg, clock, acct.user_id)
    apk = make_apk(tmp_path / "picked.apk")
    desktop = width >= 700
    shots = Path(__file__).resolve().parents[1] / ".work"   # снимки для просмотра глазами, вне git
    shots.mkdir(exist_ok=True)
    playwright, executable = local_chromium
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    user_agent=("Mozilla/5.0 (Windows NT 10.0) Chrome/130.0" if desktop
                                                else "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile/15E148"))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            web.client.headers["user-agent"] = page.evaluate("navigator.userAgent")   # iPhone — без QR у «Установить»
            page.route("https://apps.example/**", _route_to(web.client))
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                for path in ("/?os=apple", "/blocked", "/android", "/android/add", "/settings", "/help"):
                    page.goto("https://apps.example" + path)
                    if path == "/android/add":
                        page.get_by_role("navigation", name="Способ добавления").get_by_role("link", name="Файл").click()
                        page.locator('#add-file input[type="file"]').set_input_files(apk)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (path, theme)
                    for element in page.locator(NEW_TEXT).all():
                        if element.is_visible():
                            ratio = element.evaluate(CONTRAST)
                            assert ratio >= 4.5, (path, theme, element.inner_text()[:40], ratio)
                    name = path.strip("/").replace("/", "-").replace("?os=", "") or "catalog"
                    page.screenshot(path=str(shots / f"pages-{name}-{width}-{theme}.png"), full_page=True)

            # Apple: подсказка новичку закрывается и не возвращается; «?» — диалог установки
            page.goto("https://apps.example/?os=apple")
            tip = page.get_by_role("complementary", name="Как ставить с полки")
            assert tip.is_visible()
            tip.get_by_role("button", name="Скрыть подсказку «Как ставить с полки»").click()
            page.reload()
            assert tip.is_hidden()
            page.get_by_role("button", name="Как установить").first.click()
            dialog = page.get_by_role("dialog", name="Как установить на iPhone")
            assert dialog.is_visible() and "Сгрузить приложение" in dialog.inner_text()
            assert dialog.evaluate("d => d.scrollWidth <= d.clientWidth")
            page.keyboard.press("Escape")
            assert dialog.is_hidden()
            page.get_by_label("Действия с Test App", exact=True).click()
            assert page.locator(".more[open]").get_by_role("button", name="Скопировать ссылку").is_visible()
            page.keyboard.press("Escape")
            if desktop:
                page.get_by_role("button", name="Показать QR-код для Test App").click()
                assert page.get_by_alt_text("QR-код установки Test App на iPhone").is_visible()

            # «Добавить»: раздел запомнен, выбранный файл назван; галочка APKPure — только для его адреса
            page.goto("https://apps.example/android/add")
            assert page.locator("#add-file").is_visible() and page.locator("#add-search").is_hidden()
            page.locator('#add-file input[type="file"]').set_input_files(apk)
            assert page.locator("[data-file-name]").inner_text().startswith("picked.apk · ")
            page.get_by_role("navigation", name="Способ добавления").get_by_role("link", name="Ссылка").click()
            risk = page.get_by_text("Понимаю риск APKPure", exact=False)
            assert risk.is_hidden()
            page.get_by_label("Ссылка на приложение или APK", exact=False).fill("https://apkpure.com/k-9-mail/com.fsck.k9")
            assert risk.is_visible()

            # «Удалённые»: по умолчанию — что есть в аккаунте; остальное — фильтром
            page.goto("https://apps.example/blocked")
            rows = page.locator(".card.list .row")
            assert rows.count() == 1 and "Test App" in rows.first.inner_text()
            page.get_by_role("link", name="Нет в аккаунте · 1").click()
            assert "ВТБ Онлайн" in rows.first.inner_text() and rows.count() == 1
            assert not errors
        finally:
            browser.close()
