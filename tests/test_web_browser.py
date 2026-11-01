"""Real local Chromium checks for compact shelf interactions and accessible contrast."""
from urllib.parse import urlsplit

import pytest

from helpers import seed_app
from test_fourpda import local_chromium
from test_web_context import android_app


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
