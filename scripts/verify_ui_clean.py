import time

from playwright.sync_api import sync_playwright


def main():
    print("Testing clean review UI...")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        page.goto("http://127.0.0.1:8000/", timeout=15000)
        page.wait_for_selector("button:has-text('Reviews')", timeout=10000)

        # Click Clean Refactor
        clean_btn = page.locator("button:has-text('Clean Refactor')")
        clean_btn.click()
        time.sleep(0.5)

        # Click Review code
        submit_btn = page.locator("button:has-text('Review code')")
        submit_btn.click()

        # Wait for review results (Verdict banner) - real LLM multi-agent takes ~50s
        page.wait_for_selector("h2:has-text('APPROVE'), h2:has-text('REQUEST CHANGES'), h2:has-text('ESCALATE')", timeout=90000)
        time.sleep(1)

        # Check for removed elements
        has_trace = page.locator("text='Multi-Agent Execution Trace'").count() > 0
        has_timing_scope = page.locator("text='timing_scope'").count() > 0
        has_incomplete_alert = page.locator("text='Analysis incomplete. Required analyzers failed'").count() > 0
        has_completeness_details = page.locator("text='Analysis completeness and verification'").count() > 0
        has_grouped_counts = page.locator("text='Counts are grouped by source'").count() > 0
        has_scope_disclaimer = page.locator("text='Review performed via heuristic regex pattern scanning'").count() > 0
        has_unavailable_usage = page.locator("text='Usage: UNAVAILABLE - cost estimate'").count() > 0

        print(f"Multi-Agent Execution Trace present: {has_trace} (expected False)")
        print(f"Timing scope JSON present: {has_timing_scope} (expected False)")
        print(f"Incomplete alert box present: {has_incomplete_alert} (expected False)")
        print(f"Completeness details dropdown present: {has_completeness_details} (expected False)")
        print(f"Grouped counts disclaimer present: {has_grouped_counts} (expected False)")
        print(f"Scope disclaimer present: {has_scope_disclaimer} (expected False)")
        print(f"Unavailable usage subtitle present: {has_unavailable_usage} (expected False)")

        screenshot_path = r"screenshots/clean_review_results.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved to: {screenshot_path}")

        browser.close()

        assert not has_trace, "Trace visualizer still present!"
        assert not has_incomplete_alert, "Incomplete alert still present!"
        assert not has_completeness_details, "Completeness details still present!"
        assert not has_grouped_counts, "Grouped counts disclaimer still present!"
        assert not has_scope_disclaimer, "Scope disclaimer still present!"
        assert not has_unavailable_usage, "Unavailable usage subtitle still present!"
        print("ALL VERIFICATIONS PASSED!")

if __name__ == "__main__":
    main()
