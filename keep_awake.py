import time
from playwright.sync_api import sync_playwright

APP_URL = "https://truth-procurement-auditor-611.streamlit.app/"


def wake_streamlit_app():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        print(f"Opening {APP_URL} ...")
        page.goto(APP_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # If Streamlit put the app to sleep, click the wake-up button
        wake_button = page.locator("button:has-text('Yes, get this app back up')")
        if wake_button.count() > 0:
            print("App was sleeping. Clicking wake-up button...")
            wake_button.first.click()
            page.wait_for_timeout(30000)

        # Wait for the active Streamlit container so the WebSocket stays open
        try:
            page.wait_for_selector(
                "iframe, [data-testid='stAppViewContainer']", timeout=60000
            )
            print("Active Streamlit WebSocket session established.")
            time.sleep(10)
        except Exception as err:
            print(f"Warning: Container check timed out ({err}), wake signal sent.")
        finally:
            browser.close()


if __name__ == "__main__":
    wake_streamlit_app()
