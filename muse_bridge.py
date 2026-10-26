#!/usr/bin/env python3
"""MuseBridge: a local API connecting Muse web to Claude Code.

Playwright manages isolated login sessions and quota selection. The Messages API
adapter preserves conversation context, images and client tool calls; Claude Code
executes workspace operations with its own permissions.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from playwright.sync_api import (
        sync_playwright,
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
    )
    from muse_protocol import estimate_input_tokens, handle_messages
    from muse_accounts import AccountPool, QuotaExhaustedError, WeeklyQuotaExhaustedError, account_state_path, parse_subscription_response, quota_alert, saved_accounts
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc.name}. "
        "Run: pip install -r requirements.txt && playwright install chromium"
    )

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "storage_state.json"
CONFIG_FILE = BASE_DIR / "bridge_config.json"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 600
POLL_INTERVAL_S = 0.5
SETTLE_POLLS = 4

DEFAULT_CONFIG: Dict[str, Any] = {
    "chat_url": "",
    "port": DEFAULT_PORT,
    "headless": True,
    "proxy": "",  # e.g. "http://127.0.0.1:1080" or "socks5://127.0.0.1:1080"
    "cdp_url": "",  # e.g. "http://127.0.0.1:9222" to attach to your real browser
    "geolocation": {
        "latitude": 40.7128,
        "longitude": -74.0060,
    },
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "selectors": {
        "composer": "",
        "send_button": "",
        "message_list": "",
        "message": "",
    },
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
                    if "selectors" in loaded and isinstance(loaded["selectors"], dict):
                        cfg["selectors"] = {**DEFAULT_CONFIG["selectors"], **loaded["selectors"]}
        except Exception as e:
            print(f"Warning: could not read {CONFIG_FILE.name}: {e}", file=sys.stderr)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to save {CONFIG_FILE.name}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DOM Auto-Discovery Engine
# ---------------------------------------------------------------------------

AUTO_DETECT_JS = """
(() => {
    function isVisible(el) {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function getCssPath(el) {
        if (!(el instanceof Element)) return '';
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.getAttribute('data-testid')) return `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
        if (el.getAttribute('aria-label')) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(el.getAttribute('aria-label'))}"]`;

        let path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE && el.tagName.toLowerCase() !== 'body' && el.tagName.toLowerCase() !== 'html') {
            let selector = el.tagName.toLowerCase();
            if (el.className && typeof el.className === 'string') {
                const classes = el.className.trim().split(/\\s+/).filter(c => !c.startsWith('!') && !c.includes(':') && !c.includes('[') && c.length < 30);
                if (classes.length > 0) {
                    selector += '.' + CSS.escape(classes[0]);
                }
            }
            let sib = el, nth = 1;
            while (sib = sib.previousElementSibling) {
                if (sib.tagName.toLowerCase() === el.tagName.toLowerCase()) nth++;
            }
            if (nth > 1) selector += `:nth-of-type(${nth})`;
            path.unshift(selector);
            el = el.parentElement;
            if (path.length > 4) break;
        }
        return path.join(' > ');
    }

    // 1. Find Composer
    const composerCandidates = [
        'textarea',
        'div[contenteditable="true"]',
        '[role="textbox"]',
        'input[type="text"]'
    ];
    let composerEl = null;
    let composerSel = '';
    for (const sel of composerCandidates) {
        const els = Array.from(document.querySelectorAll(sel)).filter(isVisible);
        if (els.length > 0) {
            // Sort by lowest on the screen (chat composers are at bottom)
            els.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
            composerEl = els[0];
            if (composerEl.tagName.toLowerCase() === 'textarea') {
                composerSel = 'textarea';
            } else if (composerEl.getAttribute('contenteditable') === 'true') {
                composerSel = 'div[contenteditable="true"]';
            } else if (composerEl.getAttribute('role') === 'textbox') {
                composerSel = '[role="textbox"]';
            } else {
                composerSel = getCssPath(composerEl);
            }
            break;
        }
    }

    // 2. Find Send Button
    let sendBtnEl = null;
    let sendBtnSel = '';
    if (composerEl) {
        const compRect = composerEl.getBoundingClientRect();
        // Look in composer container or adjacent elements
        const container = composerEl.closest('form, div:has(button)') || document.body;
        const btns = Array.from(container.querySelectorAll('button, [role="button"]')).filter(isVisible);
        for (const b of btns) {
            const label = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
            const rect = b.getBoundingClientRect();
            // button near the composer
            const isNear = Math.abs(rect.top - compRect.top) < 150;
            if (label.includes('attach') || label.includes('upload')) continue;
            if (label.includes('send') || label.includes('submit') || b.getAttribute('type') === 'submit') {
                if (isNear) {
                    sendBtnEl = b;
                    if (b.getAttribute('aria-label')) {
                        sendBtnSel = `button[aria-label="${b.getAttribute('aria-label')}"]`;
                    } else if (b.getAttribute('type') === 'submit') {
                        sendBtnSel = 'button[type="submit"]';
                    } else {
                        sendBtnSel = getCssPath(b);
                    }
                    break;
                }
            }
        }
    }

    // 3. Find Messages & Message List
    let messageSel = '';
    let messageListSel = '';

    // Check common message selectors
    const msgPatterns = [
        '.hatch-chat-groupable-bubble',
        '[data-role="assistant"], [data-role="user"]',
        '[data-testid*="message" i]',
        'div[class*="message-bubble" i]',
        'div[class*="ChatMessage" i]',
        'div[class*="message" i]',
        'article',
        '[role="article"]'
    ];
    for (const pat of msgPatterns) {
        const matches = Array.from(document.querySelectorAll(pat)).filter(isVisible);
        if (matches.length >= 1) {
            messageSel = pat;
            const parent = matches[0].parentElement;
            if (parent) {
                messageListSel = getCssPath(parent) || '[role="log"], main';
            }
            break;
        }
    }

    return {
        url: window.location.href,
        composer: composerSel || 'textarea, div[contenteditable="true"], [role="textbox"]',
        send_button: sendBtnSel || 'button[type="submit"], button[aria-label*="send" i]',
        message_list: messageListSel || '[role="log"], main, div[class*="chat" i]',
        message: messageSel || '.hatch-chat-groupable-bubble, [data-role="assistant"], [data-role="user"]',
        has_composer: !!composerEl,
        has_send_button: !!sendBtnEl
    };
})()
"""


def detect_selectors(page: Page) -> Dict[str, Any]:
    try:
        data = page.evaluate(AUTO_DETECT_JS)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"Warning: DOM auto-detection encountered: {e}", file=sys.stderr)
    return {
        "url": page.url,
        "composer": "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]",
        "send_button": "button[type=\"submit\"], button[aria-label*=\"send\" i]",
        "message_list": "[role=\"log\"], main, div[class*=\"chat\" i]",
        "message": ".hatch-chat-groupable-bubble, [data-role=\"assistant\"], [data-role=\"user\"]",
    }


def _fill_composer(page: Page, composer_selector: str, text: str) -> None:
    """Robustly fill the composer element, triggering React/Next.js state updates."""
    sel = composer_selector or "textarea[placeholder='Message'], textarea"
    loc = page.locator(sel).last
    loc.wait_for(state="visible", timeout=15_000)
    loc.click()
    page.wait_for_timeout(200)

    try:
        loc.evaluate("el => { el.focus(); el.select(); }")
    except Exception:
        pass

    # For shorter text without newlines, keyboard typing directly triggers full React input state
    if len(text) < 1200 and "\n" not in text:
        try:
            page.keyboard.type(text)
        except Exception:
            page.keyboard.insert_text(text)
    else:
        try:
            page.keyboard.insert_text(text)
        except Exception:
            page.keyboard.type(text)

    # Wait for the Send button to become visible/enabled (confirming React state caught up)
    try:
        page.wait_for_selector('button[aria-label="Send"], button[aria-label*="Send" i]', timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _click_send(page: Page, send_btn_selector: str, composer_selector: str) -> None:
    """Click send button and ensure prompt is submitted via Enter key."""
    clicked = False
    send_candidates = [
        'button[aria-label="Send"]',
        'button[aria-label*="Send" i]',
        send_btn_selector,
    ]
    for s in send_candidates:
        if not s:
            continue
        try:
            btn = page.locator(s).last
            if btn.is_visible() and btn.is_enabled():
                print(f"[*] Clicking send button: {s}", file=sys.stderr, flush=True)
                btn.click(timeout=2000)
                clicked = True
                break
        except Exception as ex:
            print(f"[*] Send button candidate {s} failed: {ex}", file=sys.stderr, flush=True)

    # If send button wasn't clicked, or if composer still holds text, press Enter to submit
    try:
        page.wait_for_timeout(400)
        composer_loc = page.locator(composer_selector or "textarea").last
        val = composer_loc.input_value() if composer_loc.count() > 0 else ""
        if val or not clicked:
            print("[*] Submitting via Enter key...", file=sys.stderr, flush=True)
            composer_loc.press("Enter")
    except Exception:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    page.wait_for_timeout(600)


def _attach_images(page: Page, attachments: List[Dict[str, Any]]) -> None:
    """Attach actual image bytes; clear leftover drafts before each request."""
    remove = page.get_by_role("button", name="Remove attachment", exact=True)
    for _ in range(remove.count()):
        remove.first.click(timeout=5000)
    if not attachments:
        return
    file_input = page.locator('input[type="file"]').last
    if file_input.count():
        file_input.set_input_files(attachments, timeout=15000)
    else:
        with page.expect_file_chooser(timeout=15000) as chooser:
            page.get_by_role("button", name="Attach file", exact=True).last.click()
        chooser.value.set_files(attachments, timeout=15000)
    page.wait_for_function(
        """names => {
            const remove = document.querySelectorAll('button[aria-label="Remove attachment"]');
            return remove.length === names.length && names.every(name =>
                Array.from(document.images).some(img =>
                    img.alt === name && img.complete && img.naturalWidth > 0));
        }""",
        arg=[item["name"] for item in attachments],
        timeout=30000,
    )


def _extract_messages(page: Page, message_selector: str) -> List[Dict[str, str]]:
    """Extract message items as list of {'author': str, 'text': str}."""
    sel = message_selector or ".hatch-chat-groupable-bubble"
    try:
        loc = page.locator(sel)
        count = loc.count()
        if count == 0:
            loc = page.locator('.hatch-chat-groupable-bubble, [data-role="assistant"], [data-role="user"]')
            count = loc.count()
    except Exception:
        return []

    out = []
    for i in range(count):
        el = loc.nth(i)
        try:
            # Note: Do NOT filter with is_visible() here because long chat containers
            # clip off-screen bubbles, but inner_text() remains accessible in the DOM.
            text = el.inner_text().strip()
            if not text:
                continue
            classes = el.get_attribute("class") or ""
            is_user = (
                "bg-chat-user-bubble" in classes
                or "user" in classes.lower()
                or text.lstrip().startswith("You:")
                or text.lstrip().startswith("You\n")
            )
            role = "user" if is_user else "assistant"
            clean_text = text
            if is_user:
                clean_text = re.sub(r"^You\s*:\s*", "", clean_text, flags=re.IGNORECASE).strip()
            out.append({"author": role, "text": clean_text})
        except Exception:
            continue
    return out


def _is_generating(page: Page) -> bool:
    """Check if Muse is currently generating, thinking, or streaming."""
    try:
        stop_btn = page.locator("button[aria-label*='Stop' i], button:has-text('Stop'), [data-testid*='stop' i]")
        for i in range(stop_btn.count()):
            if stop_btn.nth(i).is_visible():
                return True
    except Exception:
        pass

    try:
        active_indicators = page.locator(
            "[role='log'] [class*='typing'], [role='log'] [class*='thinking'], "
            "[role='log'] [aria-label*='thinking' i], [role='log'] [aria-label*='loading' i], "
            ".hatch-chat-groupable-bubble [class*='animate-pulse']"
        )
        for i in range(min(active_indicators.count(), 5)):
            if active_indicators.nth(i).is_visible():
                return True
    except Exception:
        pass

    return False


def _wait_for_reply(
    page: Page,
    message_selector: str,
    n_before: int = 0,
    timeout_s: int = DEFAULT_TIMEOUT,
    prompt_text: str = "",
) -> str:
    """Wait for assistant response to stream and settle."""
    deadline = time.time() + timeout_s
    last_text = None
    stable_count = 0
    seen_generating = False

    # Clean target snippet from prompt_text to match in user message bubble
    target_snippet = prompt_text.strip()[:40].strip().lower() if prompt_text else ""

    print(f"[*] Waiting for Muse AI response (timeout={timeout_s}s)...", file=sys.stderr, flush=True)
    poll_count = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        poll_count += 1
        _raise_quota_alert(page)

        # 1. Scroll window and scrollable containers to bottom so new elements render
        try:
            page.evaluate("""() => {
                const scroller = document.getElementById('hatch-chat-scroll') || document.querySelector('[class*="overflow-y-auto"]');
                if (scroller) {
                    scroller.scrollTop = scroller.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
                const scrollables = document.querySelectorAll('[role="log"], main, div[class*="chat" i], div[class*="overflow" i], div[class*="scroll" i]');
                for (const el of scrollables) {
                    if (el.scrollHeight > el.clientHeight) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
            }""")
        except Exception:
            pass

        # 2. Extract current messages in the DOM
        msgs = _extract_messages(page, message_selector)
        if not msgs:
            continue

        # 3. Locate the user message for this prompt
        matched_user_idx = None
        if target_snippet:
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx].get("author") == "user":
                    u_txt = msgs[idx].get("text", "").strip().lower()
                    if target_snippet in u_txt or u_txt[:40] in target_snippet:
                        matched_user_idx = idx
                        break

        if matched_user_idx is None:
            # Fallback to the latest user message found in msgs
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx].get("author") == "user":
                    matched_user_idx = idx
                    break

        # 4. Extract assistant messages that appear AFTER the matched user message
        assistant_msgs = []
        if matched_user_idx is not None and matched_user_idx < len(msgs) - 1:
            assistant_msgs = [
                m["text"] for m in msgs[matched_user_idx + 1 :]
                if m.get("author") == "assistant" or not m.get("text", "").lstrip().startswith("You:")
            ]
        elif not assistant_msgs and n_before and len(msgs) > n_before:
            new_msgs = msgs[n_before:]
            assistant_msgs = [m["text"] for m in new_msgs if m.get("author") == "assistant"]
            if not assistant_msgs and len(new_msgs) >= 2:
                assistant_msgs = [new_msgs[-1]["text"]]

        if not assistant_msgs:
            if poll_count % 8 == 0:
                print(f"[*] Waiting for assistant bubble... (DOM bubbles={len(msgs)}, n_before={n_before}, matched_user={matched_user_idx})", file=sys.stderr, flush=True)
                try:
                    conn_elem = page.locator("text='Still sending', text='Connecting...'")
                    if conn_elem.count() > 0 and conn_elem.first.is_visible():
                        print(
                            "\n[!] ALERT: Muse session expired or disconnected ('Connecting...' / 'Still sending' detected).\n"
                            "    Your login session in storage_state.json has expired.\n"
                            "    Please refresh it by running: python muse_bridge.py login\n",
                            file=sys.stderr,
                            flush=True,
                        )
                except Exception:
                    pass
            continue

        BUBBLE_SEP = "\n\n<!-- bubble -->\n\n"
        cur_text = BUBBLE_SEP.join(m.strip() for m in assistant_msgs if m.strip())

        is_gen = _is_generating(page)
        if is_gen:
            seen_generating = True

        if poll_count % 8 == 0:
            print(f"[*] Stream settling ({poll_count * POLL_INTERVAL_S:.1f}s): bubbles={len(msgs)}, assistant_msgs={len(assistant_msgs)}, chars={len(cur_text)}, is_gen={is_gen}, seen_gen={seen_generating}, stable={stable_count}/{SETTLE_POLLS}", file=sys.stderr, flush=True)

        # 5. Settle check:
        # If Muse was actively generating and has now stopped (Stop button disappeared), and text is stable:
        if seen_generating and not is_gen and cur_text and cur_text == last_text:
            print(f"[*] Muse AI generation completed ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
            return cur_text

        # Tool arguments must be complete; a pause during generation is not EOF.
        if is_gen:
            stable_count = 0
        elif cur_text and cur_text == last_text:
            stable_count += 1
            if stable_count >= SETTLE_POLLS:
                print(f"[*] Muse AI response complete ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
                return cur_text
        else:
            stable_count = 0

        last_text = cur_text

    raise RuntimeError(f"Muse generation did not finish within {timeout_s}s; incomplete output was discarded")


def _raise_quota_alert(page: Page) -> None:
    try:
        alerts = page.locator('[role="alert"], [data-sonner-toast]')
        texts = [alerts.nth(i).inner_text() for i in range(alerts.count())
                 if alerts.nth(i).is_visible() and not alerts.nth(i).evaluate(
                     "el => !!el.closest('.hatch-chat-groupable-bubble, [data-role=assistant], [data-role=user]')")]
    except Exception:
        return
    for text in texts:
        if quota_alert(text):
            error = WeeklyQuotaExhaustedError if re.search(r"weekly|hàng tuần", text, re.IGNORECASE) else QuotaExhaustedError
            raise error("Muse displayed a quota-exhausted notice")


def _read_page_quota(page: Page) -> dict:
    """Use the website's own Settings request so rotating action IDs need no scraping."""
    try:
        page.get_by_role("button", name="Settings", exact=True).last.click(timeout=5000)
        menu = page.get_by_role("menu")
        with page.expect_response(
            lambda response: response.request.method == "POST" and "includeAgreement" in (response.request.post_data or ""),
            timeout=15000,
        ) as response:
            menu.get_by_text("Settings", exact=True).click(timeout=5000)
        if response.value.status != 200:
            raise RuntimeError(f"Muse quota lookup returned HTTP {response.value.status}")
        return parse_subscription_response(response.value.text())
    finally:
        close = page.get_by_role("dialog").get_by_role("button", name="Close", exact=True)
        if close.count():
            close.last.click(timeout=5000)
        elif page.get_by_role("menu").count():
            page.keyboard.press("Escape")


def _save_session(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name == ".muse_accounts":
        path.parent.chmod(0o700)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".session-", suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump(context.storage_state(), temp)
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _launch_browser_and_context(
    p,
    cfg: Dict[str, Any],
    headless: bool = True,
    with_storage: bool = True,
    state_file: Optional[Path] = None,
) -> Tuple[Browser, BrowserContext]:
    """Launch browser with built-in location spoofing (mimicking Location Guard), proxy, and optional CDP."""
    cdp_url = cfg.get("cdp_url")
    if cdp_url:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            sys.exit(
                f"\n[Error] Could not connect to Chrome at {cdp_url} ({e}).\n\n"
                f"Why did this happen?\n"
                f"  Chrome was not running with remote debugging enabled.\n\n"
                f"How to fix this:\n"
                f"  Option 1: If you have a desktop VPN app running (Nord, Proton, etc.),\n"
                f"            you DO NOT need --cdp! Just run:\n"
                f"                python muse_bridge.py login\n\n"
                f"  Option 2: If your VPN is a Chrome extension, launch Chrome with debugging:\n"
                f"                python muse_bridge.py launch-chrome\n"
                f"            Then in another terminal run:\n"
                f"                python muse_bridge.py login --cdp http://127.0.0.1:9222\n"
            )
        contexts = browser.contexts
        ctx = contexts[0] if contexts else browser.new_context()
        return browser, ctx

    launch_kwargs: Dict[str, Any] = {"headless": headless}
    proxy = cfg.get("proxy")
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    browser = p.chromium.launch(**launch_kwargs)

    # Location spoofing matching Location Guard + US Locale/Timezone
    ctx_kwargs: Dict[str, Any] = {
        "geolocation": cfg.get("geolocation", {"latitude": 40.7128, "longitude": -74.0060}),
        "permissions": ["geolocation"],
        "locale": cfg.get("locale", "en-US"),
        "timezone_id": cfg.get("timezone_id", "America/New_York"),
        "accept_downloads": True,
    }
    if state_file is None:
        names = saved_accounts(BASE_DIR)
        state_file = account_state_path(BASE_DIR, names[0]) if names else STATE_FILE
    if with_storage and state_file.exists():
        ctx_kwargs["storage_state"] = str(state_file)

    ctx = browser.new_context(**ctx_kwargs)
    return browser, ctx


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> None:
    """Headed login flow: opens browser, logs in, auto-discovers selectors."""
    cfg = load_config()
    account = getattr(args, "account", "default")
    state_path = account_state_path(BASE_DIR, account)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp
    if account != "default" and cfg.get("cdp_url"):
        sys.exit("Named accounts require isolated browser sessions; remove cdp_url and omit --cdp")

    target_url = args.url or (cfg.get("chat_url") if account == "default" else "") or "https://muse.ai"

    print("=" * 60)
    print("MUSEBRIDGE LOGIN & SETUP")
    print(f"Account profile: {account}")
    print("=" * 60)
    print("Launching Chromium browser window...")
    print(f"Browser locale: {cfg.get('locale', 'en-US')}; timezone: {cfg.get('timezone_id', 'America/New_York')}")
    if cfg.get("proxy"):
        print(f"  [✓] Proxy: {cfg['proxy']}")
    if cfg.get("cdp_url"):
        print(f"  [✓] Attaching via CDP to: {cfg['cdp_url']}")
    print("1. Log in to your Muse account.")
    print("2. Open the chat you want to use with Claude Code.")
    print("3. Return here and press ENTER.")
    print("-" * 60)

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=False, with_storage=False)
        page = ctx.new_page()
        page.goto(target_url)

        try:
            input("\n--> Press ENTER here after logging in and opening your chat: ")
        except (KeyboardInterrupt, EOFError):
            print("\nLogin cancelled.")
            browser.close()
            return

        # Auto-detect selectors from current page
        print("\nAnalyzing page DOM and discovering selectors...")
        detected = detect_selectors(page)

        if not any(page.locator('textarea, [contenteditable="true"]').nth(i).is_visible()
                   for i in range(page.locator('textarea, [contenteditable="true"]').count())):
            browser.close()
            sys.exit("No chat composer found. Sign in and open a chat before saving this account.")
        _save_session(ctx, state_path)

        # Update config
        final_url = page.url
        if account == "default":
            cfg["chat_url"] = final_url
        elif not cfg.get("chat_url"):
            cfg["chat_url"] = "https://muse.ai/thread/new"
        cfg["selectors"]["composer"] = detected.get("composer", "")
        cfg["selectors"]["send_button"] = detected.get("send_button", "")
        cfg["selectors"]["message_list"] = detected.get("message_list", "")
        cfg["selectors"]["message"] = detected.get("message", "")
        save_config(cfg)

        print(f"\n[OK] Session saved for account '{account}'")
        print(f"[OK] Chat URL saved: {final_url}")
        print("[OK] Discovered selectors:")
        print(f"     Composer:    {cfg['selectors']['composer']}")
        print(f"     Send Button: {cfg['selectors']['send_button']}")
        print(f"     Messages:    {cfg['selectors']['message']}")
        print(f"[OK] Configuration written to {CONFIG_FILE.name}")
        print("\nYou can now test sending messages with:")
        print('    python muse_bridge.py send "hello"')
        browser.close()


def _ensure_configured(cfg: Dict[str, Any]) -> str:
    if not saved_accounts(BASE_DIR):
        sys.exit(
            "No saved login session found.\n"
            "Please run: python muse_bridge.py login"
        )
    chat_url = cfg.get("chat_url", "").strip()
    if not chat_url or chat_url.startswith("PASTE_"):
        sys.exit(
            "No chat_url configured.\n"
            "Please run: python muse_bridge.py login"
        )
    return chat_url


def _query_daemon(endpoint: str, data: Optional[dict] = None, port: int = DEFAULT_PORT) -> Optional[dict]:
    """Check if the local background daemon is running and respond."""
    url = f"http://127.0.0.1:{port}{endpoint}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def cmd_accounts(args: argparse.Namespace) -> None:
    status = _query_daemon("/accounts", port=args.port)
    if not status:
        status = {"active_account": None, "accounts": [{"name": name} for name in saved_accounts(BASE_DIR)]}
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return
    for account in status["accounts"]:
        print(f"{'*' if account['name'] == status.get('active_account') else '-'} {account['name']}")
    if not status["accounts"]:
        print("No saved accounts. Run: python muse_bridge.py login --account NAME")


def cmd_quota(args: argparse.Namespace) -> None:
    status = _query_daemon("/quota" + ("?all=1" if args.all else ""), port=args.port)
    if not status:
        sys.exit("Could not read quota. Start 'python muse_bridge.py serve' and check your saved login sessions.")
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return
    for account in status["accounts"]:
        quota = account.get("quota") or {}
        active = " (active)" if account.get("active") else ""
        print(f"{account['name']}{active}: {quota.get('plan') or 'unknown plan'} — {quota.get('usage_label') or 'quota not checked'}")
        if quota.get("additional_tokens_label"):
            print(f"  Additional tokens: {quota['additional_tokens_label']}")
        if quota.get("resets_at"):
            print(f"  Reset (UTC): {datetime.fromtimestamp(quota['resets_at'], timezone.utc).isoformat()}")
        if quota.get("error"):
            print(f"  {quota['error']}")


def cmd_send(args: argparse.Namespace) -> None:
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    # 1. Try sending through running daemon (fast mode)
    daemon_health = _query_daemon("/health", port=port)
    if daemon_health and daemon_health.get("status") == "ok":
        resp = _query_daemon("/send", {"text": args.text, "timeout": args.timeout}, port=port)
        if resp and resp.get("status") == "ok":
            reply = resp.get("reply", "")
            if args.json:
                print(json.dumps({"reply": reply}, ensure_ascii=False))
            else:
                print(reply)
            return
        sys.exit("The running bridge request failed or timed out. Check the server log and 'python muse_bridge.py quota --all'; the prompt was not resent.")

    # 2. Fallback: launch one-shot headless browser
    chat_url = args.chat_url or _ensure_configured(cfg)
    headless = not args.headful if args.headful else cfg.get("headless", True)
    selectors = cfg.get("selectors", {})
    composer_sel = selectors.get("composer") or "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]"
    send_sel = selectors.get("send_button") or "button[type=\"submit\"], button[aria-label*=\"send\" i]"
    msg_sel = selectors.get("message") or "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article"

    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    print(f"[*] Launching browser (headless={headless})...", file=sys.stderr, flush=True)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        print(f"[*] Navigating to {chat_url}...", file=sys.stderr, flush=True)
        page.goto(chat_url, timeout=45_000)

        print("[*] Waiting for composer...", file=sys.stderr, flush=True)
        try:
            page.wait_for_selector(composer_sel, timeout=30_000)
        except PlaywrightTimeoutError:
            detected = detect_selectors(page)
            composer_sel = detected.get("composer") or composer_sel
            send_sel = detected.get("send_button") or send_sel
            msg_sel = detected.get("message") or msg_sel

        # Ensure existing chat history is loaded before calculating n_before
        try:
            page.wait_for_selector(msg_sel, timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        n_before = len(_extract_messages(page, msg_sel))
        print(f"[*] Found {n_before} messages in thread. Submitting prompt...", file=sys.stderr, flush=True)
        _fill_composer(page, composer_sel, args.text)
        _click_send(page, send_sel, composer_sel)
        print("[*] Message submitted. Waiting for Muse AI response...", file=sys.stderr, flush=True)

        reply = _wait_for_reply(page, msg_sel, n_before, timeout_s=args.timeout, prompt_text=args.text)

        if args.json:
            print(json.dumps({"reply": reply}, ensure_ascii=False))
        else:
            print(reply)
        browser.close()


def cmd_read(args: argparse.Namespace) -> None:
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    # 1. Try daemon
    resp = _query_daemon(f"/read?limit={args.limit}", port=port)
    if resp and resp.get("status") == "ok":
        print(json.dumps(resp.get("messages", []), indent=2, ensure_ascii=False))
        return

    # 2. One-shot browser
    chat_url = args.chat_url or _ensure_configured(cfg)
    headless = not args.headful if args.headful else cfg.get("headless", True)
    msg_sel = cfg.get("selectors", {}).get("message") or ".hatch-chat-groupable-bubble, [data-hatch-assistant-message-body='true']"

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector(msg_sel, timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        msgs = _extract_messages(page, msg_sel)
        print(json.dumps(msgs[-args.limit :], indent=2, ensure_ascii=False))
        browser.close()


def cmd_inspect(args: argparse.Namespace) -> None:
    """Inspect the chat DOM and take a diagnostic screenshot."""
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    print("Launching browser to inspect DOM at:", chat_url)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=not args.headful, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector("textarea", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        detected = detect_selectors(page)
        screenshot_path = BASE_DIR / "debug_screenshot.png"
        page.screenshot(path=str(screenshot_path))

        print("\n--- Detected Elements ---")
        print(f"Page Title:   {page.title()}")
        print(f"Page URL:     {page.url}")
        print(f"Composer:     {detected.get('composer')}")
        print(f"Send Button:  {detected.get('send_button')}")
        print(f"Message List: {detected.get('message_list')}")
        print(f"Message:      {detected.get('message')}")
        print(f"\nScreenshot saved to: {screenshot_path}")

        msg_sel = cfg.get("selectors", {}).get("message") or detected.get("message", "")
        msgs = _extract_messages(page, msg_sel)
        print(f"Extracted {len(msgs)} messages:")
        for m in msgs[-3:]:
            snippet = m["text"].replace("\n", " ")[:80]
            print(f"  - [{m.get('author') or 'unknown'}]: {snippet}...")

        browser.close()


def _extract_archive(archive_path: Path, cwd: str) -> List[str]:
    """Safely extract tar.gz or zip archive into target directory, flattening single top-level folder if present."""
    target_dir = Path(cwd)
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted_names: List[str] = []

    # 1. TAR Archive (.tar.gz, .tgz, .tar)
    if tarfile.is_tarfile(str(archive_path)):
        with tarfile.open(str(archive_path), "r:*") as tar:
            members = tar.getmembers()
            prefixes = {m.name.split("/")[0] for m in members if "/" in m.name}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for member in members:
                orig_name = member.name
                if single_root and (orig_name == single_root or orig_name.startswith(single_root + "/")):
                    parts = orig_name.split("/")
                    if len(parts) > 1:
                        member.name = "/".join(parts[1:])
                    else:
                        continue
                if member.name:
                    try:
                        tar.extract(member, path=str(target_dir), filter=getattr(tarfile, "data_filter", None))
                    except TypeError:
                        tar.extract(member, path=str(target_dir))
                    if not member.isdir():
                        extracted_names.append(member.name)

    # 2. ZIP Archive (.zip)
    elif zipfile.is_zipfile(str(archive_path)):
        with zipfile.ZipFile(str(archive_path), "r") as zf:
            infolist = zf.infolist()
            prefixes = {info.filename.split("/")[0] for info in infolist if "/" in info.filename}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for info in infolist:
                orig_name = info.filename
                dest_name = orig_name
                if single_root and orig_name.startswith(single_root + "/"):
                    dest_name = orig_name[len(single_root) + 1 :]
                if dest_name and not info.is_dir():
                    dest_path = target_dir / dest_name
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_names.append(dest_name)

    return extracted_names


_DOWNLOADED_ARCHIVES: set = set()


def _download_and_extract_recent_archives(page: Page, cwd: str, n_before: int = 0) -> List[str]:
    """Detect downloadable archive cards attached by Muse in the current turn, download and extract to cwd."""
    if not cwd or not os.path.isdir(cwd):
        return []

    extracted: List[str] = []
    try:
        msg_sel = ".hatch-chat-groupable-bubble"
        all_bubbles = page.locator(msg_sel)
        total_bubbles = all_bubbles.count()
        if total_bubbles == 0:
            return []

        # Only look inside newly generated bubbles from this turn
        start_idx = max(0, n_before) if n_before < total_bubbles else max(0, total_bubbles - 1)
        target_bubbles = [all_bubbles.nth(i) for i in range(start_idx, total_bubbles)]

        options_btn = None
        for b in reversed(target_bubbles):
            btns = b.locator("[data-testid='hatch-sandbox-file-card-options']")
            if btns.count() > 0 and btns.last.is_visible():
                options_btn = btns.last
                break

        if not options_btn:
            return []

        card_container = options_btn.locator("xpath=./ancestor::span[contains(@class, 'rounded') or contains(@class, 'hatch-chat-groupable-bubble')][1]")
        card_text = card_container.inner_text().lower() if card_container.count() > 0 else ""
        if not any(ext in card_text for ext in (".tar", ".zip", ".tgz", ".gz", "archive")):
            return []

        if any(arch in card_text for arch in _DOWNLOADED_ARCHIVES):
            return []

        print("[*] Detected downloadable project archive in Muse chat; triggering download...", file=sys.stderr, flush=True)
        options_btn.click()
        page.wait_for_timeout(600)

        download_item = page.locator(
            "[role='menuitem']:has-text('Download'), [data-slot='dropdown-menu-item']:has-text('Download'), button:has-text('Download')"
        ).first
        if not download_item.is_visible():
            return []

        tmp_dir = Path(tempfile.gettempdir()) / "muse_downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive_path = tmp_dir / f"project_archive_{int(time.time())}.tar.gz"

        with page.expect_download(timeout=30000) as dl_info:
            download_item.click()
        dl = dl_info.value
        suggested_name = dl.suggested_filename
        if suggested_name:
            archive_path = tmp_dir / suggested_name
        dl.save_as(str(archive_path))
        _DOWNLOADED_ARCHIVES.add(archive_path.name.lower())
        if suggested_name:
            _DOWNLOADED_ARCHIVES.add(suggested_name.lower())
        print(f"[*] Successfully downloaded archive: {archive_path.name} ({archive_path.stat().st_size} bytes)", file=sys.stderr, flush=True)

        extracted = _extract_archive(archive_path, cwd)
        print(f"[*] Extracted {len(extracted)} files directly into {cwd}", file=sys.stderr, flush=True)
    except Exception as ex:
        print(f"[!] Archive download/extraction encountered: {ex}", file=sys.stderr, flush=True)

    return extracted


# ---------------------------------------------------------------------------
# Background Daemon (HTTP / OpenAI-Compatible Server)
# ---------------------------------------------------------------------------

class BrowserManager:
    """Manages Playwright on a dedicated worker thread for thread-safe HTTP request handling."""

    def __init__(self, chat_url: str, headless: bool = True):
        self.chat_url = chat_url
        self.headless = headless
        self.task_queue: queue.Queue = queue.Queue()
        self.ready_event = threading.Event()
        self.lock = threading.Lock()
        self.url = ""
        self.error: Optional[Exception] = None
        self.thread: Optional[threading.Thread] = None
        self.accounts = AccountPool(BASE_DIR)

    @property
    def active_account(self) -> Optional[str]:
        return self.accounts.active

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready_event.wait(timeout=60):
            raise RuntimeError("Muse browser startup timed out")
        if self.error:
            raise self.error

    def _run(self) -> None:
        cfg = load_config()
        selectors = cfg.get("selectors", {})
        composer_sel = selectors.get("composer") or "textarea[placeholder='Message'], textarea"
        send_sel = selectors.get("send_button") or 'button[aria-label="Send"], button[aria-label*="Send" i]'
        msg_sel = selectors.get("message") or ".hatch-chat-groupable-bubble"

        default_chat_url = self.chat_url
        try:
            if cfg.get("cdp_url") and len(saved_accounts(BASE_DIR)) > 1:
                raise RuntimeError("Multi-account mode requires isolated browsers; remove cdp_url from bridge_config.json")
            with sync_playwright() as p:
                browser = ctx = page = None
                opened_account = None
                opened_version = None

                def session_version(name):
                    state = account_state_path(BASE_DIR, name).stat()
                    return state.st_mtime_ns, state.st_size

                known_versions = {name: session_version(name) for name in saved_accounts(BASE_DIR)}

                def open_account(name):
                    if cfg.get("cdp_url") and (name != "default" or len(saved_accounts(BASE_DIR)) > 1):
                        raise RuntimeError("Multiple accounts cannot share a CDP browser; remove cdp_url")
                    selected_browser, selected_ctx = _launch_browser_and_context(
                        p, cfg, headless=self.headless, with_storage=True,
                        state_file=account_state_path(BASE_DIR, name),
                    )
                    try:
                        selected_page = selected_ctx.new_page()
                        selected_page.goto(default_chat_url if name == "default" else "https://muse.ai/thread/new", timeout=60_000)
                        selected_page.wait_for_selector(composer_sel, timeout=30_000)
                        selected_page.wait_for_timeout(3000)
                        return selected_browser, selected_ctx, selected_page
                    except Exception as exc:
                        selected_browser.close()
                        raise RuntimeError(f"Account '{name}' could not open a chat; run login --account {name}") from exc

                def persist():
                    nonlocal opened_version
                    if ctx is not None and opened_account is not None:
                        try:
                            # A newer interactive login must win over this warm browser's cookies.
                            if session_version(opened_account) != opened_version:
                                return
                            _save_session(ctx, account_state_path(BASE_DIR, opened_account))
                            opened_version = session_version(opened_account)
                            known_versions[opened_account] = opened_version
                        except Exception:
                            print(f"[!] Could not refresh saved session for '{opened_account}'", file=sys.stderr)

                def refresh_quota(name, selected_page, force=False):
                    previous = self.accounts.quotas.get(name, {})
                    reset_due = previous.get("exhausted") and previous.get("resets_at") is not None and previous["resets_at"] <= time.time()
                    if not force and not reset_due and time.time() - previous.get("checked_at", 0) < 60:
                        return previous
                    try:
                        quota = _read_page_quota(selected_page)
                    except Exception:
                        quota = {**previous, "checked_at": time.time(), "error": "Quota unavailable; verify Muse Settings > General > Usage"}
                    self.accounts.quotas[name] = quota
                    return quota

                def use_account(name):
                    nonlocal browser, ctx, page, opened_account, opened_version
                    if name == opened_account and session_version(name) == opened_version:
                        return
                    next_version = session_version(name)
                    next_browser, next_ctx, next_page = open_account(name)
                    persist()
                    browser.close()
                    browser, ctx, page = next_browser, next_ctx, next_page
                    opened_account, opened_version = name, next_version
                    self.url = page.url
                    print(f"[Daemon] Opened Muse account '{name}'", file=sys.stderr, flush=True)

                try:
                    names = saved_accounts(BASE_DIR)
                    if not names:
                        raise RuntimeError("No saved Muse accounts; run 'python muse_bridge.py login'")
                    opened_account = self.accounts.active = names[0]
                    opened_version = session_version(opened_account)
                    print(f"[Daemon] Opening Muse account '{opened_account}'...")
                    browser, ctx, page = open_account(opened_account)
                    self.url = page.url
                    self.ready_event.set()
                    print("[Daemon] Browser warm and ready for requests.")

                    while True:
                        task = self.task_queue.get()
                        action = task[0]
                        if action == "stop":
                            break
                        elif action == "send":
                            _, text, timeout_s, res_q, req_cwd, attachments = task
                            tried: set[str] = set()
                            checked: set[str] = set()
                            deadline = time.monotonic() + timeout_s
                            try:
                                while True:
                                    if time.monotonic() >= deadline:
                                        raise RuntimeError("Muse request timed out during account selection")
                                    for candidate in saved_accounts(BASE_DIR):
                                        version = session_version(candidate)
                                        if known_versions.get(candidate) != version:
                                            self.accounts.quotas.pop(candidate, None)
                                            checked.discard(candidate)
                                            known_versions[candidate] = version
                                    names = saved_accounts(BASE_DIR)
                                    if self.accounts.phase == "weekly" and self.accounts.active in names:
                                        names.remove(self.accounts.active)
                                        names.insert(0, self.accounts.active)
                                    # Check weekly allowances before permitting any additional credits.
                                    for candidate in names:
                                        if candidate in tried:
                                            continue
                                        if candidate not in checked and self.accounts.available(candidate):
                                            use_account(candidate)
                                            refresh_quota(candidate, page, force=True)
                                            checked.add(candidate)
                                        if self.accounts.weekly_available(candidate) is True:
                                            break
                                    name = self.accounts.select(tried)
                                    use_account(name)
                                    if time.monotonic() >= deadline:
                                        raise RuntimeError("Muse request timed out during account selection")
                                    try:
                                        _raise_quota_alert(page)
                                        n_before = len(_extract_messages(page, msg_sel))
                                        _attach_images(page, attachments)
                                        _fill_composer(page, composer_sel, text)
                                        _click_send(page, send_sel, composer_sel)
                                        reply = _wait_for_reply(page, msg_sel, n_before, timeout_s=max(1, int(deadline - time.monotonic())), prompt_text=text)
                                        extracted = _download_and_extract_recent_archives(page, req_cwd, n_before=n_before)
                                        self.url = page.url
                                        persist()
                                        res_q.put(("ok", (reply, extracted)))
                                        break
                                    except QuotaExhaustedError as exc:
                                        weekly_only = isinstance(exc, WeeklyQuotaExhaustedError) and self.accounts.phase == "weekly"
                                        self.accounts.exhausted(name, weekly_only=weekly_only)
                                        if not weekly_only:
                                            tried.add(name)
                                        print(f"[Daemon] Account '{name}' exhausted {'weekly' if weekly_only else 'all'} quota; selecting next allowance", file=sys.stderr, flush=True)
                            except Exception as ex:
                                res_q.put(("error", ex))
                        elif action == "quota":
                            _, all_accounts, res_q = task
                            names = saved_accounts(BASE_DIR) if all_accounts else [opened_account]
                            for name in names:
                                if name == opened_account and session_version(name) == opened_version:
                                    refresh_quota(name, page, force=True)
                                else:
                                    other_browser = None
                                    try:
                                        other_version = session_version(name)
                                        other_browser, other_ctx, other_page = open_account(name)
                                        refresh_quota(name, other_page, force=True)
                                        if session_version(name) == other_version:
                                            _save_session(other_ctx, account_state_path(BASE_DIR, name))
                                            known_versions[name] = session_version(name)
                                    except Exception:
                                        self.accounts.quotas[name] = {"error": f"Session unavailable; run login --account {name}", "checked_at": time.time()}
                                    finally:
                                        if other_browser:
                                            other_browser.close()
                            res_q.put(("ok", self.accounts.status()))
                        elif action == "read":
                            _, limit, res_q = task
                            try:
                                res_q.put(("ok", _extract_messages(page, msg_sel)[-limit:]))
                            except Exception as ex:
                                res_q.put(("error", str(ex)))
                        elif action == "navigate":
                            _, new_url, res_q = task
                            try:
                                page.goto(new_url, timeout=60_000)
                                page.wait_for_selector(composer_sel, timeout=30_000)
                                page.wait_for_timeout(3000)
                                self.url = self.chat_url = page.url
                                if opened_account == "default":
                                    default_chat_url = page.url
                                    cfg["chat_url"] = page.url
                                    save_config(cfg)
                                res_q.put(("ok", self.url))
                            except Exception as ex:
                                res_q.put(("error", str(ex)))
                finally:
                    persist()
                    if browser:
                        browser.close()
        except Exception as e:
            self.error = e
            self.ready_event.set()

    def stop(self) -> None:
        self.task_queue.put(("stop",))

    def navigate(self, new_url: str) -> str:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("navigate", new_url, res_q))
            try:
                status, val = res_q.get(timeout=60)
            except queue.Empty:
                raise RuntimeError("Muse navigation timed out.")
            if status == "error":
                if isinstance(val, Exception):
                    raise val
                raise RuntimeError(val)
            return val

    def get_quota(self, all_accounts: bool = False) -> dict:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("quota", all_accounts, res_q))
            try:
                status, value = res_q.get(timeout=max(60, 40 * len(saved_accounts(BASE_DIR))))
            except queue.Empty:
                raise RuntimeError("Muse quota lookup timed out")
            return value

    def send_prompt(self, text: str, timeout_s: int = DEFAULT_TIMEOUT, cwd: str = "", attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, List[str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("send", text, timeout_s, res_q, cwd, attachments or []))
            try:
                status, val = res_q.get(timeout=timeout_s + 30)
            except queue.Empty:
                raise RuntimeError(f"Muse prompt timed out after {timeout_s}s waiting for response.")
            if status == "error":
                if isinstance(val, Exception):
                    raise val
                raise RuntimeError(val)
            return val

    def get_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("read", limit, res_q))
            try:
                status, val = res_q.get(timeout=30)
            except queue.Empty:
                raise RuntimeError("Muse get_messages timed out.")
            if status == "error":
                raise RuntimeError(val)
            return val


def strip_system_tags(text: str) -> str:
    """Remove <system-reminder>...</system-reminder> and unwrap <pasted_content> tags injected by Claude Code."""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", r"\1", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def is_title_generation_query(prompt: str) -> bool:
    """Detect if prompt is an automated session title generation query (Claude Code, Continue, etc.)."""
    p_lower = prompt.lower()
    return bool(
        "<session>" in p_lower
        or "write the title in the predominant language" in p_lower
        or ("session title" in p_lower and "title:" in p_lower)
        or re.search(r"\b(?:generate|write|create)\s+(?:a\s+)?(?:short\s+)?(?:session\s+)?title\b", p_lower)
    )


def generate_local_session_title(text: str) -> str:
    """Generate a clean, concise 3-5 word title locally from Claude Code's session prompt."""
    m = re.search(r"<session>(.*?)</session>", text, flags=re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    raw = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|i\s+want\s+to\s+)", "", raw, flags=re.IGNORECASE)
    m_fn = re.search(r"\b([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_]{1,6})\b", raw)
    words = [w for w in raw.split() if w]
    if len(words) <= 5:
        title = " ".join(words)
    elif m_fn:
        title = f"{words[0].capitalize()} {m_fn.group(1)}"
    else:
        title = " ".join(words[:5])
    title = title.strip()[:50]
    return title[0].upper() + title[1:] if title else "Coding Session"


def create_daemon_server(mgr: BrowserManager, port: int) -> ThreadingHTTPServer:
    class BridgeHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[HTTP] {self.address_string()} - {fmt % args}\n")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _send_json(self, status: int, data: Any):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def _send_sse(self, event: str, data: dict):
            line = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(line)
            self.wfile.flush()

        def do_GET(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            if clean_path == "/health":
                self._send_json(200, {"status": "ok", "url": mgr.url or "", "active_account": getattr(mgr, "active_account", None)})
            elif clean_path == "/accounts":
                self._send_json(200, mgr.accounts.status())
            elif clean_path == "/quota":
                try:
                    self._send_json(200, mgr.get_quota(all_accounts="all=1" in self.path))
                except Exception as exc:
                    self._send_json(502, {"status": "error", "error": str(exc)})
            elif clean_path == "/chat_url":
                self._send_json(200, {"url": mgr.url or mgr.chat_url})
            elif clean_path.startswith("/read"):
                limit = 10
                if "limit=" in self.path:
                    try:
                        limit = int(self.path.split("limit=")[1].split("&")[0])
                    except ValueError:
                        pass
                msgs = mgr.get_messages(limit=limit)
                self._send_json(200, {"status": "ok", "messages": msgs})
            elif clean_path in ("/v1/models", "/models", "/api/v1/models", "/api/models"):
                models_list = [
                    {"id": "muse", "type": "model", "object": "model", "display_name": "Muse AI"},
                    {"id": "stealth/union-alpha", "type": "model", "object": "model", "display_name": "Stealth Union Alpha"},
                    {"id": "claude-3-7-sonnet", "type": "model", "object": "model", "display_name": "Claude 3.7 Sonnet"},
                    {"id": "claude-3-5-sonnet-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Sonnet"},
                    {"id": "claude-3-5-haiku-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Haiku"},
                ]
                self._send_json(200, {"data": models_list})
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len < 0 or content_len > 20 * 1024 * 1024:
                    self._send_json(413, {"type": "error", "error": {"type": "invalid_request_error", "message": "Request body exceeds 20 MiB"}})
                    self.close_connection = True
                    return
                raw_body = self.rfile.read(content_len).decode("utf-8") if content_len else "{}"
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object")
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})
                return

            if clean_path == "/chat_url":
                new_url = payload.get("url") or payload.get("chat_url", "")
                if not new_url:
                    self._send_json(400, {"status": "error", "error": "Missing 'url' parameter"})
                    return
                try:
                    final_url = mgr.navigate(new_url)
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/reset":
                try:
                    final_url = mgr.navigate("https://muse.ai/")
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/send":
                text = payload.get("text") or payload.get("prompt", "")
                if not text:
                    self._send_json(400, {"status": "error", "error": "Missing 'text' parameter"})
                    return
                timeout_s = payload.get("timeout", DEFAULT_TIMEOUT)
                try:
                    reply, extracted = mgr.send_prompt(text, timeout_s=timeout_s, cwd=os.getcwd())
                    self._send_json(200, {"status": "ok", "reply": reply, "extracted_files": extracted})
                except Exception as e:
                    self._send_json(429 if isinstance(e, QuotaExhaustedError) else 500, {"status": "error", "error": str(e)})

            elif clean_path in ("/v1/messages/count_tokens", "/messages/count_tokens", "/api/v1/messages/count_tokens"):
                try:
                    self._send_json(200, {"input_tokens": estimate_input_tokens(payload)})
                except ValueError as exc:
                    self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})

            elif clean_path in ("/v1/messages", "/messages", "/api/v1/messages", "/api/messages"):
                messages = payload.get("messages", [])
                last = messages[-1] if isinstance(messages, list) and messages and isinstance(messages[-1], dict) else {}
                content = last.get("content", "")
                has_results = False
                if isinstance(content, list):
                    has_results = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                    text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
                else:
                    text = content if isinstance(content, str) else ""
                local_reply = None
                if not has_results:
                    if "[SUGGESTION MODE:" in text:
                        local_reply = ""
                    elif is_title_generation_query(text):
                        local_reply = generate_local_session_title(strip_system_tags(text))
                handle_messages(self, mgr, payload, local_reply=local_reply)

            elif self.path in ("/v1/chat/completions", "/chat/completions"):
                # OpenAI Compatible Chat Endpoint
                messages = payload.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break
                if not last_user_msg and messages:
                    last_user_msg = messages[-1].get("content", "")

                if not last_user_msg:
                    self._send_json(400, {"error": "No user message found"})
                    return

                try:
                    reply, _ = mgr.send_prompt(last_user_msg)
                    resp = {
                        "id": f"chatcmpl-muse-{int(time.time())}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": payload.get("model", "muse"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": reply,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(last_user_msg.split()),
                            "completion_tokens": len(reply.split()),
                            "total_tokens": len(last_user_msg.split()) + len(reply.split()),
                        },
                    }
                    self._send_json(200, resp)
                except Exception as e:
                    self._send_json(500, {"error": str(e)})
            else:
                self._send_json(404, {"error": "Endpoint not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), BridgeHTTPHandler)


def run_daemon_server(mgr: BrowserManager, port: int) -> None:
    server = create_daemon_server(mgr, port)
    print(f"[Daemon] HTTP Server listening at http://127.0.0.1:{port}")
    print(f"  - Health check:            http://127.0.0.1:{port}/health")
    print(f"  - Anthropic API (Claude):  POST http://127.0.0.1:{port}/v1/messages")
    print(f"  - OpenAI API:              POST http://127.0.0.1:{port}/v1/chat/completions")
    print(f"  - Send endpoint:           POST http://127.0.0.1:{port}/send")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


def cmd_serve(args: argparse.Namespace) -> None:
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    port = args.port or cfg.get("port", DEFAULT_PORT)
    headless = not args.headful if args.headful else cfg.get("headless", True)

    mgr = BrowserManager(chat_url=chat_url, headless=headless)
    try:
        mgr.start()
        run_daemon_server(mgr, port)
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# MCP Server Mode (Model Context Protocol over stdio for Claude Code)
# ---------------------------------------------------------------------------

def cmd_mcp(args: argparse.Namespace) -> None:
    """Run an MCP server over stdio for Claude Code."""
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    def send_rpc(obj: dict):
        line = json.dumps(obj)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        req_id = req.get("id")
        method = req.get("method")

        if method == "initialize":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "muse-bridge", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "ask_muse",
                            "description": "Send a question or task to Meta Muse AI and receive its answer.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "prompt": {
                                        "type": "string",
                                        "description": "The message or prompt to send to Muse",
                                    }
                                },
                                "required": ["prompt"],
                            },
                        },
                        {
                            "name": "read_muse_chat",
                            "description": "Read recent chat messages from the Muse AI session.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {
                                        "type": "integer",
                                        "description": "Number of recent messages to return (default: 10)",
                                        "default": 10,
                                    }
                                },
                            },
                        },
                    ]
                },
            })
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})

            if name == "ask_muse":
                prompt = arguments.get("prompt", "")
                # Query daemon if running, else one-shot
                reply_text = ""
                daemon_res = _query_daemon("/send", {"text": prompt}, port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    reply_text = daemon_res.get("reply", "")
                else:
                    # One-shot fallback
                    try:
                        chat_url = _ensure_configured(cfg)
                        msg_sel = cfg.get("selectors", {}).get("message") or "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article"
                        comp_sel = cfg.get("selectors", {}).get("composer") or "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]"
                        send_sel = cfg.get("selectors", {}).get("send_button") or "button[type=\"submit\"], button[aria-label*=\"send\" i]"

                        with sync_playwright() as p:
                            browser, ctx = _launch_browser_and_context(p, cfg, headless=True, with_storage=True)
                            page = ctx.new_page()
                            page.goto(chat_url, timeout=45_000)
                            page.wait_for_selector(comp_sel, timeout=30_000)
                            n_before = len(_extract_messages(page, msg_sel))
                            _fill_composer(page, comp_sel, prompt)
                            _click_send(page, send_sel, comp_sel)
                            reply_text = _wait_for_reply(page, msg_sel, n_before)
                            browser.close()
                    except Exception as ex:
                        reply_text = f"Error communicating with Muse: {ex}"

                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": reply_text}],
                        "isError": False,
                    },
                })

            elif name == "read_muse_chat":
                limit = arguments.get("limit", 10)
                daemon_res = _query_daemon(f"/read?limit={limit}", port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    msgs = daemon_res.get("messages", [])
                else:
                    msgs = []
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(msgs, indent=2)}],
                        "isError": False,
                    },
                })
            else:
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                })


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="MuseBridge: connect Muse web to Claude Code through a local API."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # login
    p_login = sub.add_parser("login", help="Headed login & automatic selector discovery")
    p_login.add_argument("--url", default=None, help="Custom chat or login URL (default: https://muse.ai)")
    p_login.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_login.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_login.add_argument("--account", default="default", help="Saved account profile name (default: existing storage_state.json)")
    p_login.set_defaults(fn=cmd_login)

    p_accounts = sub.add_parser("accounts", help="List saved Muse account profiles (* marks the active profile)")
    p_accounts.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_accounts.add_argument("--json", action="store_true")
    p_accounts.set_defaults(fn=cmd_accounts)

    p_quota = sub.add_parser("quota", help="Read real account usage and reset time from Muse Settings")
    p_quota.add_argument("--all", action="store_true", help="Refresh quota for every saved account")
    p_quota.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_quota.add_argument("--json", action="store_true")
    p_quota.set_defaults(fn=cmd_quota)

    # send
    p_send = sub.add_parser("send", help="Send a prompt and return the assistant reply")
    p_send.add_argument("text", help="Message or prompt to send")
    p_send.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds to wait for complete reply")
    p_send.add_argument("--json", action="store_true", help="Output reply as JSON")
    p_send.add_argument("--chat-url", default=None, help="Override chat URL")
    p_send.add_argument("--headful", action="store_true", help="Show browser window for debugging")
    p_send.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_send.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_send.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_send.set_defaults(fn=cmd_send)

    # read
    p_read = sub.add_parser("read", help="Print recent chat messages as JSON")
    p_read.add_argument("--limit", type=int, default=10, help="Number of messages to fetch")
    p_read.add_argument("--chat-url", default=None, help="Override chat URL")
    p_read.add_argument("--headful", action="store_true", help="Show browser window")
    p_read.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_read.add_argument("--proxy", default=None, help="Proxy server")
    p_read.add_argument("--cdp", default=None, help="Connect via CDP")
    p_read.set_defaults(fn=cmd_read)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Diagnose chat DOM selectors and capture a debug screenshot")
    p_inspect.add_argument("--chat-url", default=None, help="Override chat URL")
    p_inspect.add_argument("--headful", action="store_true", help="Show browser window")
    p_inspect.add_argument("--proxy", default=None, help="Proxy server")
    p_inspect.add_argument("--cdp", default=None, help="Connect via CDP")
    p_inspect.set_defaults(fn=cmd_inspect)

    # serve (background daemon)
    p_serve = sub.add_parser("serve", help="Run the local API with browser sessions and account rotation")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    p_serve.add_argument("--chat-url", default=None, help="Override chat URL")
    p_serve.add_argument("--headful", action="store_true", help="Show browser window while running")
    p_serve.add_argument("--proxy", default=None, help="Proxy server")
    p_serve.add_argument("--cdp", default=None, help="Connect via CDP")
    p_serve.set_defaults(fn=cmd_serve)

    # launch-chrome
    p_chrome = sub.add_parser("launch-chrome", help="Launch Chrome with remote debugging enabled")
    p_chrome.add_argument("--port", type=int, default=9222, help="Debugging port (default: 9222)")
    p_chrome.add_argument("--url", default="https://muse.ai", help="Initial URL")
    p_chrome.set_defaults(fn=cmd_launch_chrome)

    # mcp (Model Context Protocol)
    p_mcp = sub.add_parser("mcp", help="Run as Model Context Protocol (MCP) server for Claude Code")
    p_mcp.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to query daemon from")
    p_mcp.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    args.fn(args)


def cmd_launch_chrome(args: argparse.Namespace) -> None:
    """Launch Google Chrome (or Edge) with remote debugging enabled on Windows."""
    import subprocess
    port = args.port or 9222
    url = args.url or "https://muse.ai"
    user_data = Path(os.environ.get("LOCALAPPDATA", ".")) / "Google" / "Chrome" / "User Data Debug"

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_exe = None
    for c in candidates:
        if os.path.exists(c):
            browser_exe = c
            break

    if not browser_exe:
        sys.exit("Error: Could not find Chrome or Edge executable on this machine.")

    cmd = [
        browser_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        url,
    ]
    print(f"Launching browser with remote debugging on port {port}...")
    subprocess.Popen(cmd)
    print(f"\n[OK] Browser launched!")
    print(f"1. Log in to Muse and open your chat in the opened Chrome window.")
    print(f"2. Then run:")
    print(f"    python muse_bridge.py login --cdp http://127.0.0.1:{port}")


if __name__ == "__main__":
    main()
