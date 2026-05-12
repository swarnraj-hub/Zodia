import asyncio
import os
import random
import re
import sys
import time
from datetime import date, timedelta

import boto3
import pyotp
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://trade-uk.zodiamarkets.com/login"

S3_BUCKET = "payout-recon"
S3_PREFIX = "zodia/fx/raw/"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [1, 2, 3, 4, 5];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }
});
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
"""


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[ERROR] Environment variable {name!r} is not set.")
    return value


async def human_type(locator, text, wpm=60):
    delay_ms = int(60_000 / (wpm * 5))
    for char in text:
        await locator.type(char, delay=delay_ms + random.randint(-10, 30))


async def fill_otp_boxes(page, otp_code):
    boxes = await page.query_selector_all('[id^="two-fa-"]')
    if len(boxes) >= 6:
        for i, digit in enumerate(otp_code[:6]):
            await boxes[i].focus()
            await boxes[i].fill(digit)
            await asyncio.sleep(random.uniform(0.08, 0.18))
        print("[*] OTP entered into two-fa-* inputs")
        return

    boxes = await page.query_selector_all('input[class*="min-w-"]')
    visible = [b for b in boxes if await b.is_visible()]
    if len(visible) >= 6:
        for i, digit in enumerate(otp_code[:6]):
            await visible[i].focus()
            await visible[i].fill(digit)
            await asyncio.sleep(random.uniform(0.08, 0.18))
        print("[*] OTP entered into min-w fallback inputs")
        return

    raise RuntimeError("Could not find OTP inputs.")


async def click_otp_submit(page):
    await asyncio.sleep(0.5)

    buttons = await page.query_selector_all("button")
    print(f"[debug] {len(buttons)} button(s) on OTP page:")
    for i, b in enumerate(buttons):
        txt = (await b.text_content() or "").strip()
        cls = (await b.get_attribute("class") or "")[:60]
        print(f"  [{i}] text={txt!r} class={cls}")

    for btn_text in ["Verify", "Submit", "Confirm", "Continue", "Next", "Sign In"]:
        btn = page.locator(f'button:has-text("{btn_text}")').last
        if await btn.count():
            el = await btn.element_handle()
            await page.evaluate("el => el.click()", el)
            print(f"[*] JS-clicked OTP submit: '{btn_text}'")
            return

    for b in reversed(buttons):
        cls = (await b.get_attribute("class") or "")
        if "bg-primary" in cls:
            txt = (await b.text_content() or "").strip()
            await page.evaluate("el => el.click()", b)
            print(f"[*] JS-clicked bg-primary button: {txt!r}")
            return

    if buttons:
        await page.evaluate("el => el.click()", buttons[-1])
        print("[*] JS-clicked last button on page")


async def pick_date(page, target: date) -> None:
    date_id = target.strftime("id-%Y-%m-%d")
    cell = await page.query_selector(f'[class*="{date_id}"]')
    if not cell:
        raise RuntimeError(f"Calendar cell not found for {target} ('{date_id}')")

    box = await cell.bounding_box()
    if not box:
        raise RuntimeError(f"Calendar cell for {target} has no bounding box")

    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    await page.mouse.click(x, y)
    await asyncio.sleep(0.3)
    print(f"[*] Mouse-clicked {target} at ({x:.0f}, {y:.0f})")


async def export_csv(page) -> str:
    today = date.today()
    start = today - timedelta(days=10)

    print("[*] Clicking Reports button ...")
    await page.locator('button:has-text("Reports")').click()
    await asyncio.sleep(1.0)

    print("[*] Opening Time Period calendar ...")
    await page.locator(
        'input[placeholder="Time Period"], [placeholder*="Time Period"]'
    ).first.click()
    await asyncio.sleep(1.0)

    print(f"[*] Selecting start date: {start} ...")
    await pick_date(page, start)
    await asyncio.sleep(0.6)

    print(f"[*] Selecting end date: {today} ...")
    await pick_date(page, today)
    await asyncio.sleep(0.8)

    # Dismiss calendar by clicking the modal header
    await page.locator("#dialogs").get_by_text("Reports").first.click()
    await asyncio.sleep(0.5)

    # Wait for Export CSV to become enabled
    export_btn = page.locator('button:has-text("Export CSV")')
    print("[*] Waiting for Export CSV to become enabled ...")
    for _ in range(30):
        cls = await export_btn.get_attribute("class") or ""
        if "pointer-events-none" not in cls and "opacity-50" not in cls:
            break
        await asyncio.sleep(0.3)
    else:
        await page.screenshot(path="export_btn_debug.png")
        print("[!] Export CSV still disabled — screenshot saved -> export_btn_debug.png")

    print("[*] Clicking Export CSV ...")
    async with page.expect_download(timeout=30_000) as dl_info:
        el = await export_btn.element_handle()
        await page.evaluate("el => el.click()", el)

    download = await dl_info.value
    filename = f"zodia_transactions_{start}_{today}.csv"

    temp_path = await download.path()
    print(f"[*] Download captured at temp path: {temp_path}")

    upload_to_s3(temp_path, filename)

    await download.delete()
    print("[*] Temp file deleted.")


def upload_to_s3(temp_path: str, filename: str) -> None:
    aws_key    = _require_env("AWS_ACCESS_KEY_ID")
    aws_secret = _require_env("AWS_SECRET_ACCESS_KEY")

    s3_key = S3_PREFIX + filename

    print(f"[*] Uploading to s3://{S3_BUCKET}/{s3_key} ...")
    s3 = boto3.client(
        "s3",
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        region_name="ap-southeast-1",
    )
    s3.upload_file(temp_path, S3_BUCKET, s3_key)
    print(f"[+] Upload complete -> s3://{S3_BUCKET}/{s3_key}")


async def login():
    username    = _require_env("ZODIA_USERNAME")
    password    = _require_env("ZODIA_PASSWORD")
    totp_secret = _require_env("ZODIA_TOTP_SECRET")

    totp = pyotp.TOTP(totp_secret)

    async with async_playwright() as pw:
        headless = os.environ.get("ZODIA_HEADLESS", "false").lower() == "true"
        browser = await pw.chromium.launch(
            headless=headless,
            slow_mo=50,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            timezone_id="Europe/London",
        )

        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        print("[*] Loading login page ...")
        await page.goto(LOGIN_URL, wait_until="networkidle")

        print("[*] Entering credentials ...")
        username_input = page.locator('input[placeholder="Enter username"]')
        await username_input.click()
        await human_type(username_input, username)
        await asyncio.sleep(random.uniform(0.3, 0.7))

        password_input = page.locator('input[placeholder="Enter password"]')
        await password_input.click()
        await human_type(password_input, password)
        await asyncio.sleep(random.uniform(0.2, 0.5))

        print("[*] Clicking Sign In ...")
        await page.locator('button:has-text("Sign In")').click()

        print("[*] Waiting for OTP screen ...")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        print(f"[*] Current URL: {page.url}")
        await asyncio.sleep(2.0)

        remaining = 30 - (int(time.time()) % 30)
        if remaining < 4:
            print(f"[*] TOTP window expires in {remaining}s - waiting for next window ...")
            await asyncio.sleep(remaining + 1)

        otp_code = totp.now()
        print(f"[*] Entering TOTP code {otp_code} ...")
        await fill_otp_boxes(page, otp_code)

        await click_otp_submit(page)

        print("[*] Waiting for portfolio page ...")
        try:
            await page.wait_for_url("**/portfolio**", timeout=20_000)
            print("[+] Logged in successfully!")
        except PlaywrightTimeoutError:
            print(f"[!] Portfolio URL not reached. Current URL: {page.url}")
            await page.screenshot(path="login_result.png")
            print("[*] Screenshot saved -> login_result.png")
            input("\nPress Enter to close the browser ...")
            await browser.close()
            return

        await export_csv(page)

        await browser.close()
        print("[*] Browser closed.")


if __name__ == "__main__":
    asyncio.run(login())