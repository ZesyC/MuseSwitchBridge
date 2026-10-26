"""Opt-in live Muse image test: python tests/image_smoke.py.

Uploads a generated test image with a random code absent from the prompt, then
checks a follow-up request that only references the image in conversation history.
"""

import base64
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from muse_bridge import BrowserManager, create_daemon_server, load_config


def main():
    marker = "VISION-" + uuid.uuid4().hex[:8].upper()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('<div style="background:white;color:black;font:bold 64px monospace;padding:48px;width:900px">' + marker + '</div>')
        data = page.locator("div").screenshot()
        browser.close()
    manager = BrowserManager(load_config()["chat_url"], headless=True)
    manager.start()
    server = create_daemon_server(manager, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "model": "muse",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": "Đọc dòng chữ trong ảnh đính kèm và trả lời duy nhất chính xác dòng chữ đó. Không dùng công cụ."},
        ]}],
    }
    try:
        for turn in range(2):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/messages",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as exc:
                with exc:
                    raise RuntimeError(exc.read().decode()) from exc
            answer = "".join(block.get("text", "") for block in result["content"]).strip()
            assert answer == marker, f"Image recognition failed on turn {turn + 1}: {answer!r}"
            assert result["stop_reason"] == "end_turn", result
            payload["messages"] += [
                {"role": "assistant", "content": result["content"]},
                {"role": "user", "content": "Nhắc lại chính xác dòng chữ trong ảnh đã gửi."},
            ]
            print(f"PASS: live Muse image recognition, turn {turn + 1}; random code read correctly.", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        manager.stop()


if __name__ == "__main__":
    main()
