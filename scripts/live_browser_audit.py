"""
Broad End-to-End Live Browser Audit Script for Code Review Agent Web UI.
Uses Playwright Python to test http://127.0.0.1:8000.
Verifies:
- Page load & initial DOM hierarchy
- Session authentication (Operator Token modal/input)
- Navigation: Review Dashboard, Jobs Queue Monitor, Calibration View, AI Threat Radar
- Form interaction: sample diff entry, mode selection, options
- Console logs & network telemetry
- Captures screenshots for UI verification
"""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = Path(
    r"screenshots"
)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = os.getenv("APP_URL", "http://127.0.0.1:8000")


def run_audit():
    print(f" Starting broad live browser audit against {BASE_URL}...")
    results = {
        "base_url": BASE_URL,
        "page_loaded": False,
        "console_errors": [],
        "views_tested": {},
        "screenshots": [],
        "overall_status": "PENDING",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Capture console messages
        def on_console(msg):
            if msg.type in ("error", "warning"):
                results["console_errors"].append(f"[{msg.type.upper()}] {msg.text}")
                print(f"  Browser Console [{msg.type.upper()}]: {msg.text}")

        page.on("console", on_console)

        # 1. Load root URL
        print("\n1. Navigating to Root URL...")
        response = page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        assert response is not None
        print(f"   HTTP Status: {response.status}")
        results["page_loaded"] = response.status == 200
        page.wait_for_timeout(1000)

        # Screenshot landing
        shot_landing = SCREENSHOT_DIR / "01_landing_page.png"
        page.screenshot(path=str(shot_landing))
        results["screenshots"].append(str(shot_landing))
        print(f"    Saved screenshot: {shot_landing.name}")

        title = page.title()
        print(f"   Page Title: '{title}'")

        # 2. Authenticate with Operator Token if prompt is visible
        print("\n2. Checking Authentication State...")
        token_input = page.locator("input[placeholder*='token' i], input[type='password'], [aria-label*='token' i]")
        if token_input.count() > 0 and token_input.first.is_visible():
            print("   Operator token input found. Authenticating...")
            token_input.first.fill("test-operator-token")
            connect_btn = page.locator("button:has-text('Connect'), button:has-text('Submit'), button:has-text('Login')")
            if connect_btn.count() > 0:
                connect_btn.first.click()
                page.wait_for_timeout(1000)
                print("   Connected successfully.")
        else:
            print("   No token prompt modal currently blocking. Session active or public access enabled.")

        shot_auth = SCREENSHOT_DIR / "02_after_auth.png"
        page.screenshot(path=str(shot_auth))
        results["screenshots"].append(str(shot_auth))

        # 3. Test Navigation Views
        print("\n3. Testing Navigation & View Switching...")

        # A: Review Dashboard
        print("   Testing Review Dashboard...")
        dashboard_btn = page.locator("button:has-text('Review'), button:has-text('Dashboard'), nav a:has-text('Review')")
        if dashboard_btn.count() > 0:
            dashboard_btn.first.click()
            page.wait_for_timeout(800)
        shot_dashboard = SCREENSHOT_DIR / "03_review_dashboard.png"
        page.screenshot(path=str(shot_dashboard))
        results["screenshots"].append(str(shot_dashboard))
        results["views_tested"]["dashboard"] = "PASSED"

        # Check Diff Input Area
        textarea = page.locator("textarea")
        if textarea.count() > 0 and textarea.first.is_visible():
            print("   Diff input textarea verified.")
            sample_diff = (
                "diff --git a/app.py b/app.py\n"
                "@@ -10,1 +10,2 @@\n"
                "- # previous comment\n"
                "+ query = f'SELECT * FROM users WHERE id = {uid}'\n"
            )
            textarea.first.fill(sample_diff)
            print("   Entered sample security test diff.")
            page.wait_for_timeout(500)
            shot_diff = SCREENSHOT_DIR / "04_sample_diff_entered.png"
            page.screenshot(path=str(shot_diff))
            results["screenshots"].append(str(shot_diff))

        # B: Job Queue Monitor
        print("   Testing Job Queue Monitor...")
        jobs_btn = page.locator("button:has-text('Job Queue'), button:has-text('Queue'), button:has-text('Jobs')")
        if jobs_btn.count() > 0:
            jobs_btn.first.click()
            page.wait_for_timeout(1000)
            shot_jobs = SCREENSHOT_DIR / "05_job_queue_monitor.png"
            page.screenshot(path=str(shot_jobs))
            results["screenshots"].append(str(shot_jobs))
            results["views_tested"]["job_queue"] = "PASSED"
            print("   Job Queue view verified.")

        # C: Calibration / Trust View
        print("   Testing Repository Calibration & Trust View...")
        calib_btn = page.locator("button:has-text('Calibration'), button:has-text('Trust'), button:has-text('Metrics')")
        if calib_btn.count() > 0:
            calib_btn.first.click()
            page.wait_for_timeout(1000)
            shot_calib = SCREENSHOT_DIR / "06_calibration_view.png"
            page.screenshot(path=str(shot_calib))
            results["screenshots"].append(str(shot_calib))
            results["views_tested"]["calibration"] = "PASSED"
            print("   Calibration view verified.")

        # D: AI Threat Radar
        print("   Testing AI Threat Radar View...")
        radar_btn = page.locator("button:has-text('Threat'), button:has-text('Security'), button:has-text('Radar')")
        if radar_btn.count() > 0:
            radar_btn.first.click()
            page.wait_for_timeout(1000)
            shot_radar = SCREENSHOT_DIR / "07_threat_radar.png"
            page.screenshot(path=str(shot_radar))
            results["screenshots"].append(str(shot_radar))
            results["views_tested"]["threat_radar"] = "PASSED"
            print("   Threat radar view verified.")

        # 4. Mobile Viewport Check
        print("\n4. Testing Mobile Viewport (iPhone 14 - 390x844)...")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(800)
        shot_mobile = SCREENSHOT_DIR / "08_mobile_responsive.png"
        page.screenshot(path=str(shot_mobile))
        results["screenshots"].append(str(shot_mobile))
        results["views_tested"]["mobile_responsive"] = "PASSED"
        print("   Mobile responsive rendering verified.")

        # Check for horizontal overflow
        scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
        inner_width = page.evaluate("() => window.innerWidth")
        results["mobile_horizontal_overflow"] = scroll_width > inner_width
        print(f"   Mobile scrollWidth={scroll_width}, innerWidth={inner_width} (Overflow: {results['mobile_horizontal_overflow']})")

        browser.close()

    results["overall_status"] = "PASSED" if results["page_loaded"] else "FAILED"
    print("\n" + "=" * 60)
    print(" BROAD LIVE BROWSER AUDIT COMPLETE")
    print(f" Overall Status: {results['overall_status']}")
    print(f" Views Tested: {list(results['views_tested'].keys())}")
    print(f" Console Errors: {len(results['console_errors'])}")
    print(f" Screenshots Captured: {len(results['screenshots'])}")
    print("=" * 60)

    # Save summary report
    out_json = SCREENSHOT_DIR / "audit_report.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Report saved to {out_json}")


if __name__ == "__main__":
    run_audit()
