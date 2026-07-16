from __future__ import annotations

import hashlib
from pathlib import Path


def chromium_executable() -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).resolve()


def chromium_sha256() -> tuple[Path, str]:
    executable = chromium_executable()
    if not executable.is_file():
        raise FileNotFoundError("Playwright Chromium is not installed")
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return executable, digest.hexdigest()


def chromium_launch_version() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return browser.version
        finally:
            browser.close()
