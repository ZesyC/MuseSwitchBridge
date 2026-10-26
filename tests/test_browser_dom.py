"""Local browser fixtures for the Muse DOM; no login or network requests."""

import html
import json
import os
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from muse_bridge import _attach_images, _extract_messages, _launch_browser_and_context, _raise_quota_alert, _save_session, detect_selectors
from muse_accounts import QuotaExhaustedError, account_state_path
from muse_protocol import parse_reply
from test_muse_bridge import png_bytes


class BrowserDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        if not Path(cls.playwright.chromium.executable_path).exists():
            cls.playwright.stop()
            raise unittest.SkipTest("Run playwright install chromium for DOM tests")
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_empty_chat_does_not_confuse_attach_with_send(self):
        self.page.set_content('<form><textarea></textarea><button aria-label="Attach file"><svg></svg></button></form>')
        detected = detect_selectors(self.page)
        self.assertTrue(detected["has_composer"])
        self.assertNotIn("Attach", detected["send_button"])
        self.assertIn(".hatch-chat-groupable-bubble", detected["message"])

    def test_only_provider_alerts_can_trigger_quota_failover(self):
        self.page.set_content('<div class="hatch-chat-groupable-bubble"><div role="alert">You are out of tokens</div></div>')
        _raise_quota_alert(self.page)
        self.page.set_content('<div role="alert">Network connection lost</div>')
        _raise_quota_alert(self.page)
        self.page.set_content('<div role="alert">You have reached your weekly limit</div>')
        with self.assertRaises(QuotaExhaustedError):
            _raise_quota_alert(self.page)

    def test_known_bubbles_work_when_saved_selector_is_stale(self):
        self.page.set_content('<div class="hatch-chat-groupable-bubble bg-chat-user-bubble">Read a.py</div><div class="hatch-chat-groupable-bubble">reply</div>')
        self.assertEqual(_extract_messages(self.page, ".outdated-selector"), [
            {"author": "user", "text": "Read a.py"},
            {"author": "assistant", "text": "reply"},
        ])

    def test_code_block_preserves_edit_arguments_with_quotes_and_newlines(self):
        arguments = {"file_path": "/tmp/hello.py", "old_string": 'print("old")\n', "new_string": 'print("xin chào")\n'}
        envelope = {"request_id": "turn", "text": "", "tool_calls": [{"name": "Edit", "input": arguments}]}
        rendered = html.escape(json.dumps(envelope, ensure_ascii=False))
        self.page.set_content('<div class="hatch-chat-groupable-bubble"><div>json</div><pre><code>' + rendered + '</code></pre></div>')
        payload = {"tools": [{"name": "Edit", "input_schema": {"type": "object"}}]}
        extracted = _extract_messages(self.page, ".hatch-chat-groupable-bubble")
        self.assertEqual(parse_reply(extracted[0]["text"], payload, "turn")[0]["input"], arguments)

    def attachment_fixture(self, dynamic=False):
        self.page.set_content('''<div id="previews"></div><script>
            function addInput() {
                let input = document.createElement('input');
                input.type = 'file'; input.multiple = true;
                input.onchange = () => {
                    document.getElementById('previews').replaceChildren();
                    for (const file of input.files) {
                        const wrapper = document.createElement('div');
                        const img = document.createElement('img');
                        img.alt = file.name; img.src = URL.createObjectURL(file);
                        const remove = document.createElement('button');
                        remove.setAttribute('aria-label', 'Remove attachment');
                        remove.onclick = () => wrapper.remove();
                        wrapper.append(img, remove);
                        document.getElementById('previews').append(wrapper);
                    }
                };
                document.body.append(input); return input;
            }
        </script>''')
        if dynamic:
            self.page.evaluate('''() => {
                const button = document.createElement('button');
                button.setAttribute('aria-label', 'Attach file');
                button.onclick = () => addInput().click();
                document.body.append(button);
            }''')
        else:
            self.page.evaluate('addInput()')

    def test_multiple_images_are_attached_and_stale_drafts_are_cleared(self):
        self.attachment_fixture()
        first = {"name": "first.png", "mimeType": "image/png", "buffer": png_bytes()}
        second = {"name": "second.png", "mimeType": "image/png", "buffer": png_bytes((0, 0, 255))}
        _attach_images(self.page, [first, second])
        self.assertEqual(self.page.get_by_role("button", name="Remove attachment").count(), 2)
        self.assertEqual(self.page.locator('input[type=file]').evaluate('(el) => [...el.files].map(f => f.name)'), ["first.png", "second.png"])
        _attach_images(self.page, [second])
        self.assertEqual(self.page.locator('img').count(), 1)
        self.assertEqual(self.page.locator('img').get_attribute("alt"), "second.png")
        _attach_images(self.page, [])
        self.assertEqual(self.page.get_by_role("button", name="Remove attachment").count(), 0)

    def test_file_chooser_fallback_uploads_image_bytes(self):
        self.attachment_fixture(dynamic=True)
        _attach_images(self.page, [{"name": "choice.png", "mimeType": "image/png", "buffer": png_bytes()}])
        self.assertEqual(self.page.locator('img').get_attribute("alt"), "choice.png")

    def test_real_browser_cookie_sessions_are_isolated_and_saved_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("default", "backup"):
                path = account_state_path(root, name)
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({"cookies": [{"name": "test_session", "value": name, "domain": "example.test", "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []}))
                browser, context = _launch_browser_and_context(self.playwright, {}, state_file=path)
                try:
                    self.assertEqual(context.cookies()[0]["value"], name)
                    _save_session(context, path)
                    self.assertEqual(json.loads(path.read_text())["cookies"][0]["value"], name)
                    if os.name != "nt":
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                finally:
                    browser.close()
            self.assertFalse(list(root.rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
