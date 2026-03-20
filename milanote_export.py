from __future__ import annotations

"""
Milanote → Markdown exporter
Recursively exports all boards from a root board, preserving hierarchy as folders.

Requirements:
    py -m pip install playwright
    py -m playwright install chromium

Usage:
    py milanote_export.py --email YOU@EXAMPLE.COM --password SECRET \\
                          --root-url https://app.milanote.com/BOARD_ID \\
                          --mode both
"""

import argparse
import asyncio
import re
import sys
import threading
from pathlib import Path

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export Milanote boards recursively as Markdown, PNG, or both.",
    )
    p.add_argument("--email", required=True, help="Milanote login email")
    p.add_argument("--password", required=True, help="Milanote login password")
    p.add_argument(
        "--root-url", required=True, help="URL of the root board to export"
    )
    p.add_argument(
        "--output",
        default="milanote_export",
        help="Output directory (default: milanote_export)",
    )
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser in headless mode (default: True, use --no-headless to watch)",
    )
    p.add_argument(
        "--mode",
        choices=["markdown", "png", "both"],
        default="png",
        help="Export mode (default: png)",
    )
    return p.parse_args()


ARGS: argparse.Namespace  # set in main()

visited: set = set()
paused = threading.Event()
paused.set()  # start in "running" state


def _watch_keys() -> None:
    """Background thread: toggle pause when user presses P + Enter."""
    print("💡  Press P + Enter at any time to pause/resume.\n")
    while True:
        key = sys.stdin.readline().strip().lower()
        if key == "p":
            if paused.is_set():
                paused.clear()
                print("\n⏸   Paused — press P + Enter to resume.")
            else:
                paused.set()
                print("\n▶️   Resumed.")


async def wait_if_paused() -> None:
    """Await until the script is unpaused."""
    while not paused.is_set():
        await asyncio.sleep(0.5)


def slugify(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name)
    return name or "untitled"


async def login(page: Page) -> None:
    print("🔐  Logging in …")
    await page.goto("https://app.milanote.com/login", wait_until="domcontentloaded")
    await page.fill('input[type="email"]', ARGS.email)
    await page.fill('input[type="password"]', ARGS.password)
    await page.click('button[type="submit"]')
    await page.wait_for_url(re.compile(r"/1[A-Za-z0-9]{4,}"), timeout=30_000)
    await page.wait_for_selector(".workspace-ready", timeout=15_000)
    print("✅  Logged in.")


async def get_board_title(page: Page) -> str:
    """Read the current board title from the header."""
    try:
        el = page.locator(".CurrentBoardHeaderTitle .title-text").first
        title = await el.inner_text(timeout=6_000)
        return title.strip() or "untitled"
    except Exception:
        raw = await page.title()
        return raw.replace("| Milanote", "").strip() or "untitled"


async def dismiss_billing_popup(page: Page) -> None:
    """Close the 'workspace is full' billing popup if it appears."""
    try:
        close_btn = page.locator(".BillingAlertPopup .close-button").first
        await close_btn.wait_for(state="visible", timeout=3_000)
        await close_btn.click()
        await page.wait_for_timeout(400)
        print("    ℹ️  Dismissed billing popup.")
    except PWTimeout:
        pass  # popup wasn't there, carry on


async def export_current_board_as_markdown(
    page: Page, dest: Path, retries: int = 3
) -> None:
    """Click Export → Markdown and save the downloaded file."""
    dest.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        await dismiss_billing_popup(page)

        try:
            export_btn = page.locator("button.popup-trigger-export").first
            await export_btn.wait_for(state="visible", timeout=8_000)
            await export_btn.click()
            await page.wait_for_timeout(600)
        except PWTimeout:
            print("    ⚠️  Export button not found — skipping.")
            return

        try:
            async with page.expect_download(timeout=20_000) as dl_info:
                md_btn = page.locator(
                    ".ExportPopupExportButton", has_text="Markdown"
                ).first
                await md_btn.wait_for(state="visible", timeout=6_000)
                await md_btn.click()
            download = await dl_info.value
            title = await get_board_title(page)
            target = dest / (slugify(title) + ".md")
            await download.save_as(target)
            print(f"    💾  Saved → {target}")
            return  # success
        except PWTimeout:
            await page.keyboard.press("Escape")
            if attempt < retries:
                print(
                    f"    ⚠️  Download timed out (attempt {attempt}/{retries}), retrying…"
                )
                await page.wait_for_timeout(1_500)
            else:
                print(f"    ❌  Download failed after {retries} attempts — skipping.")


async def export_current_board_as_png(page: Page, dest: Path, retries: int = 3) -> None:
    """Click Export → PNG image and save the downloaded file."""
    dest.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        await dismiss_billing_popup(page)

        try:
            export_btn = page.locator("button.popup-trigger-export").first
            await export_btn.wait_for(state="visible", timeout=8_000)
            await export_btn.click()
            await page.wait_for_timeout(600)
        except PWTimeout:
            print("    ⚠️  Export button not found — skipping.")
            return

        try:
            async with page.expect_download(timeout=20_000) as dl_info:
                png_btn = page.locator(
                    ".ExportPopupExportButton", has_text="PNG image"
                ).first
                await png_btn.wait_for(state="visible", timeout=6_000)
                await png_btn.click()
            download = await dl_info.value
            title = await get_board_title(page)
            target = dest / (slugify(title) + ".png")
            await download.save_as(target)
            print(f"    💾  Saved → {target}")
            return  # success
        except PWTimeout:
            await page.keyboard.press("Escape")
            if attempt < retries:
                print(
                    f"    ⚠️  PNG download timed out (attempt {attempt}/{retries}), retrying…"
                )
                await page.wait_for_timeout(1_500)
            else:
                print(
                    f"    ❌  PNG download failed after {retries} attempts — skipping."
                )


async def already_exported(dest: Path, title: str) -> bool:
    slug = slugify(title)
    mode = ARGS.mode
    if mode == "both":
        return (dest / (slug + ".md")).exists() and (dest / (slug + ".png")).exists()
    ext = ".png" if mode == "png" else ".md"
    return (dest / (slug + ext)).exists()


async def collect_child_board_urls(page: Page) -> list:
    """
    Return list of (title, url) for every Board element on the current page.
    """
    await page.wait_for_timeout(1_500)

    results = await page.evaluate("""
        () => {
            const boards = [];
            const seen = new Set();
            document.querySelectorAll('.CanvasElement .Board, .ListElement .Board').forEach(el => {
                const wrapper = el.closest('[data-element-id]');
                if (!wrapper) return;
                const id = wrapper.getAttribute('data-element-id');
                if (seen.has(id)) return;
                seen.add(id);
                const titleEl = el.querySelector('.editable-title .title-text');
                const title = titleEl ? titleEl.innerText.trim() : id;
                boards.push({ id, title });
            });
            return boards;
        }
    """)

    output = []
    for item in results:
        eid = item.get("id", "")
        title = item.get("title", eid)
        if eid:
            output.append((title, f"https://app.milanote.com/{eid}"))

    return output


async def process_board(page: Page, url: str, dest: Path) -> None:
    """Recursively export a board and all its children."""
    await wait_if_paused()

    norm = url.rstrip("/").split("/")[-1]
    if norm in visited:
        return
    visited.add(norm)

    print(f"\n📋  Opening: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        print(f"    ⚠️  Could not load {url}: {e}")
        return

    try:
        await page.wait_for_selector(".workspace-ready", timeout=15_000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(2_500)

    title = await get_board_title(page)
    print(f"    Title: {title}")
    folder = dest / slugify(title)

    if await already_exported(folder, title):
        print(f"    ⏭️  Already exported — skipping.")
    elif ARGS.mode == "both":
        await export_current_board_as_markdown(page, folder)
        await export_current_board_as_png(page, folder)
    elif ARGS.mode == "png":
        await export_current_board_as_png(page, folder)
    else:
        await export_current_board_as_markdown(page, folder)

    children = await collect_child_board_urls(page)
    print(f"    Found {len(children)} child board(s).")

    for child_title, child_url in children:
        await process_board(page, child_url, folder)


async def main() -> None:
    global ARGS
    ARGS = parse_args()
    output_dir = Path(ARGS.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_watch_keys, daemon=True).start()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=ARGS.headless)
        ctx = await browser.new_context(accept_downloads=True)
        page = await ctx.new_page()

        await login(page)
        await process_board(page, ARGS.root_url, output_dir)

        await browser.close()

    print(f"\n🎉  Export complete! Files saved to: {output_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
