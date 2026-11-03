"""Real local Chromium checks for compact shelf interactions and accessible contrast."""
from urllib.parse import urlsplit

import pytest

from helpers import seed_app
from test_fourpda import local_chromium
from test_web_context import android_app
from appshelf.android import db, fetch, pin
from apk_factory import png_bytes


@pytest.mark.integration
@pytest.mark.parametrize("width", [320, 390, 1280])
def test_shelf_layout_dialog_keyboard_and_primary_contrast(web, conn, cfg, clock, acct, width, local_chromium):
    playwright, executable = local_chromium
    android_app(conn, cfg, clock, acct.user_id)
    seed_app(conn, cfg, clock, versions=("1.0", "2.0"))

    def route(target):
        request = target.request
        u = urlsplit(request.url)
        result = web.client.request(request.method, u.path + ("?" + u.query if u.query else ""),
                                    content=request.post_data_buffer,
                                    headers={"user-agent": request.headers.get("user-agent", "")})
        target.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    user_agent=("Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile"
                                                if width < 700 else "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0"))
            page.route("https://apps.example/**", route)
            for theme in ("light", "dark"):
                web.client.get(f"/theme?set={theme}")
                for path in ("/?os=apple", "/history", "/blocked", "/android", "/android/add", "/android/public", "/apple", "/admin", "/login", "/p"):
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
