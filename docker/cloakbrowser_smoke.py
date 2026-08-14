"""Launch a headed CloakBrowser window on DISPLAY and verify Playwright access."""

from tempfile import TemporaryDirectory

from cloakbrowser import launch_persistent_context


def main() -> None:
    with TemporaryDirectory(prefix="cloakbrowser-smoke-") as profile_dir:
        context = launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            geoip=False,
            humanize=False,
            locale="en-US",
        )
        try:
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.goto("data:text/html,<title>cloakbrowser-ok</title><h1>ok</h1>")
            result = {
                "title": page.title(),
                "user_agent": page.evaluate("navigator.userAgent"),
            }
            if result["title"] != "cloakbrowser-ok":
                raise RuntimeError(f"unexpected browser result: {result}")
            print(f"CloakBrowser headed smoke OK: {result}", flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()
