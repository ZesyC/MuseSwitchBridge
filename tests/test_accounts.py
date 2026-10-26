import json
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import muse_bridge
from muse_accounts import AccountPool, QuotaExhaustedError, WeeklyQuotaExhaustedError, account_state_path, parse_subscription_response, quota_alert, saved_accounts


class AccountPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        account_state_path(self.root, "default").write_text("{}")
        self.root.joinpath(".muse_accounts").mkdir()
        account_state_path(self.root, "backup").write_text("{}")
        self.pool = AccountPool(self.root)
        self.pool.quotas = {name: {"percent_used": 10, "additional_available": True, "checked_at": 100} for name in ("default", "backup")}

    def test_profiles_are_isolated_and_names_cannot_escape_the_directory(self):
        self.assertEqual(saved_accounts(self.root), ["default", "backup"])
        self.assertNotEqual(account_state_path(self.root, "default"), account_state_path(self.root, "backup"))
        for name in ("../other", "a/b", "", "a b", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                account_state_path(self.root, name)

    def test_sticks_to_active_account_until_exhausted_and_recovers_at_reset(self):
        self.assertEqual(self.pool.select(), "default")
        self.assertEqual(self.pool.select(), "default")
        self.pool.quotas["default"] = {"exhausted": True, "resets_at": 500}
        with patch("muse_accounts.time.time", return_value=400):
            self.assertEqual(self.pool.select(), "backup")
            self.pool.exhausted("backup")
            with self.assertRaises(QuotaExhaustedError):
                self.pool.select()
        with patch("muse_accounts.time.time", return_value=501):
            self.assertTrue(self.pool.available("default"))
            self.assertIsNone(self.pool.weekly_available("default"))
            self.pool.quotas["default"] = {"percent_used": 0, "checked_at": 501}
            self.assertEqual(self.pool.select(), "default")

    def test_new_accounts_can_be_added_without_restart(self):
        self.pool.exhausted("default")
        self.pool.exhausted("backup")
        account_state_path(self.root, "third").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "Weekly quota unavailable"):
            self.pool.select()
        self.pool.quotas["third"] = {"percent_used": 0}
        self.assertEqual(self.pool.select(), "third")

    def test_weekly_first_then_additional_from_first_account_and_reset_priority(self):
        account_state_path(self.root, "third").write_text("{}")
        self.pool.quotas["third"] = {"percent_used": 10, "additional_available": True}
        self.pool.exhausted("default", weekly_only=True)
        self.assertEqual(self.pool.select(), "backup")
        self.assertEqual(self.pool.phase, "weekly")
        self.pool.exhausted("backup", weekly_only=True)
        self.assertEqual(self.pool.select(), "third")
        self.pool.exhausted("third", weekly_only=True)
        self.assertEqual(self.pool.select(), "default")
        self.assertEqual(self.pool.phase, "additional")
        self.assertEqual(self.pool.select(), "default")
        self.pool.exhausted("default")
        self.assertEqual(self.pool.select(), "backup")
        self.pool.exhausted("backup")
        self.assertEqual(self.pool.select(), "third")
        self.pool.quotas["default"] = {"percent_used": 0, "additional_available": False}
        self.assertEqual(self.pool.select(), "default")
        self.assertEqual(self.pool.phase, "weekly")

    def test_unknown_weekly_or_additional_balance_cannot_spend_credits(self):
        self.pool.exhausted("default", weekly_only=True)
        self.pool.quotas["backup"]["error"] = "Settings failed"
        with self.assertRaisesRegex(RuntimeError, "Weekly quota unavailable"):
            self.pool.select()
        self.pool.exhausted("backup")
        self.pool.quotas["default"].pop("additional_available")
        with self.assertRaisesRegex(RuntimeError, "Additional token balance unavailable"):
            self.pool.select()

    def test_session_symlinks_cannot_alias_other_credentials(self):
        account_state_path(self.root, "linked").symlink_to(account_state_path(self.root, "default"))
        with self.assertRaises(ValueError):
            account_state_path(self.root, "linked")

    def test_subscription_response_preserves_real_fields_without_private_billing(self):
        subscription = {
            "usage": {"quotaStatus": "SUFFICIENT", "percentUsed": 100, "resetsAt": 1793487976},
            "usageRowLabel": "Free plan", "usageRowValueLabel": "100% used", "topupBalance": 40000000,
            "topupRowValueLabel": "0% used (1B tokens left)",
            "agreement": {"secret": "billing-data"}, "access_token": "credential",
        }
        quota = parse_subscription_response('0:{"a":"$@1"}\n1:' + json.dumps({"success": True, "subscription": subscription}))
        self.assertFalse(quota["exhausted"], "100% weekly usage does not prove top-up credits are exhausted")
        self.assertEqual(quota["additional_tokens_label"], "0% used (1B tokens left)")
        self.assertTrue(quota["additional_available"])
        subscription["topupBalance"] = 0
        self.assertFalse(parse_subscription_response('1:' + json.dumps({"success": True, "subscription": subscription}))["additional_available"])
        self.assertNotIn("additional_tokens", quota)
        self.assertNotIn("40000000", json.dumps(quota))
        self.assertEqual(quota["resets_at"], 1793487976)
        self.assertNotIn("billing-data", json.dumps(quota))
        self.assertNotIn("credential", json.dumps(quota))
        subscription["usage"]["quotaStatus"] = "INSUFFICIENT"
        quota = parse_subscription_response('1:' + json.dumps({"success": True, "subscription": subscription}))
        self.assertTrue(quota["exhausted"])
        with self.assertRaises(ValueError):
            parse_subscription_response('1:{"success":false}')

    def test_quota_notices_are_distinct_from_network_or_model_errors(self):
        for text in ("You've reached your weekly limit", "You've hit your weekly token limit", "Usage limit exceeded", "You're out of tokens", "Đã hết hạn mức"):
            with self.subTest(text=text):
                self.assertTrue(quota_alert(text))
        for text in ("Network connection lost", "Invalid JSON", "Too many requests. Try again shortly", "1% used", "You've reached your attachment limit", "You've reached your rate limit"):
            with self.subTest(text=text):
                self.assertFalse(quota_alert(text))

    def test_cli_does_not_resend_when_a_running_bridge_rejects_the_prompt(self):
        args = Namespace(port=8765, text="do a task", timeout=600, json=False)
        with patch.object(muse_bridge, "_query_daemon", side_effect=[{"status": "ok"}, None]), patch.object(muse_bridge, "_launch_browser_and_context") as launch:
            with self.assertRaises(SystemExit):
                muse_bridge.cmd_send(args)
            launch.assert_not_called()


class FakePage:
    def __init__(self, name):
        self.name = name
        self.url = ""

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_selector(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, timeout):
        pass


class FakeContext:
    def __init__(self, name):
        self.name = name

    def new_page(self):
        return FakePage(self.name)


class FakeBrowser:
    def close(self):
        pass


class FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class WorkerFailoverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        account_state_path(self.root, "default").write_text("{}")
        self.root.joinpath(".muse_accounts").mkdir()
        account_state_path(self.root, "backup").write_text("{}")
        self.opened = []
        self.prompts = []
        self.images = []
        self.saved = []
        self.quotas = {name: {"quota_status": "SUFFICIENT", "exhausted": False, "percent_used": 5, "additional_available": True, "checked_at": time.time()} for name in ("default", "backup")}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def launch(p, config, **kwargs):
            path = kwargs["state_file"]
            name = "default" if path.name == "storage_state.json" else path.stem
            self.opened.append((name, path))
            return FakeBrowser(), FakeContext(name)

        replacements = {
            "BASE_DIR": self.root,
            "load_config": lambda: {"selectors": {}},
            "sync_playwright": FakePlaywright,
            "_launch_browser_and_context": launch,
            "_read_page_quota": lambda page: dict(self.quotas[page.name]),
            "_save_session": lambda ctx, path: self.saved.append((ctx.name, path)),
            "_raise_quota_alert": lambda page: None,
            "_extract_messages": lambda *args: [],
            "_attach_images": lambda page, images: self.images.append((page.name, images)),
            "_fill_composer": lambda page, selector, text: self.prompts.append((page.name, text)),
            "_click_send": lambda *args: None,
            "_wait_for_reply": lambda page, *args, **kwargs: "reply from " + page.name,
            "_download_and_extract_recent_archives": lambda *args, **kwargs: [],
        }
        for key, value in replacements.items():
            self.stack.enter_context(patch.object(muse_bridge, key, value))
        self.manager = muse_bridge.BrowserManager("https://muse.ai/thread/default-task")
        self.manager.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.manager.stop()
        self.manager.thread.join(timeout=2)
        self.assertFalse(self.manager.thread.is_alive())

    def test_preflight_quota_switches_before_sending_and_preserves_images(self):
        self.quotas["default"] = {"exhausted": True, "resets_at": time.time() + 3600}
        images = [{"name": "image.png", "mimeType": "image/png", "buffer": b"test-image"}]
        self.assertEqual(self.manager.send_prompt("full context", attachments=images), ("reply from backup", []))
        self.assertEqual(self.manager.active_account, "backup")
        self.assertEqual(self.prompts, [("backup", "full context")])
        self.assertEqual(self.images, [("backup", images)])
        self.assertEqual([name for name, path in self.opened], ["default", "backup"])
        self.assertNotEqual(self.opened[0][1], self.opened[1][1])
        self.manager.send_prompt("next turn")
        self.assertEqual([name for name, _ in self.prompts], ["backup", "backup"])
        self.assertTrue(all(path == account_state_path(self.root, name) for name, path in self.saved))

    def test_mid_request_quota_retries_same_pending_prompt_once_per_account(self):
        def reply(page, *args, **kwargs):
            if page.name == "default":
                raise QuotaExhaustedError("provider quota notice")
            return "done"
        with patch.object(muse_bridge, "_wait_for_reply", reply):
            self.assertEqual(self.manager.send_prompt("pending request"), ("done", []))
        self.assertEqual(self.prompts, [("default", "pending request"), ("backup", "pending request")])

    def test_network_errors_do_not_switch_or_replay(self):
        with patch.object(muse_bridge, "_wait_for_reply", side_effect=RuntimeError("connection lost")):
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                self.manager.send_prompt("pending request")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.prompts, [("default", "pending request")])

    def test_weekly_then_additional_rotation_and_weekly_reset(self):
        self.manager.send_prompt("weekly A")
        self.quotas["default"]["percent_used"] = 100
        self.manager.send_prompt("weekly B")
        self.quotas["backup"]["percent_used"] = 100
        self.manager.send_prompt("additional A")
        self.manager.send_prompt("additional A again")
        self.quotas["default"].update(exhausted=True, additional_available=False)
        self.manager.send_prompt("additional B")
        self.quotas["backup"]["percent_used"] = 0
        self.manager.send_prompt("weekly B reset")
        self.assertEqual(self.prompts, [("default", "weekly A"), ("backup", "weekly B"), ("default", "additional A"), ("default", "additional A again"), ("backup", "additional B"), ("backup", "weekly B reset")])
        self.assertEqual(self.manager.accounts.phase, "weekly")

    def test_failed_quota_read_blocks_additional_but_allows_confirmed_weekly(self):
        self.quotas["default"]["percent_used"] = 100
        def read(page):
            if page.name == "backup":
                raise RuntimeError("Settings unavailable")
            return dict(self.quotas[page.name])
        with patch.object(muse_bridge, "_read_page_quota", read):
            with self.assertRaisesRegex(RuntimeError, "Weekly quota unavailable"):
                self.manager.send_prompt("must not spend additional")
        self.assertFalse(self.prompts)
        self.quotas["default"]["percent_used"] = 5
        self.manager.send_prompt("weekly remains usable")
        self.assertEqual(self.prompts, [("default", "weekly remains usable")])

    def test_weekly_notice_does_not_discard_additional_balance(self):
        calls = []
        def reply(page, *args, **kwargs):
            calls.append((page.name, self.manager.accounts.phase))
            if self.manager.accounts.phase == "weekly":
                raise WeeklyQuotaExhaustedError("weekly quota notice")
            return "additional works"
        with patch.object(muse_bridge, "_wait_for_reply", reply):
            self.assertEqual(self.manager.send_prompt("pending"), ("additional works", []))
        self.assertEqual(calls, [("default", "weekly"), ("backup", "weekly"), ("default", "additional")])
        self.assertTrue(self.manager.accounts.quotas["default"]["additional_available"])

    def test_new_login_reloads_active_context_without_overwriting_new_cookies(self):
        path = account_state_path(self.root, "default")
        path.write_text('{"new_login":true}')
        self.assertEqual(self.manager.send_prompt("after login"), ("reply from default", []))
        self.assertEqual([name for name, _ in self.opened], ["default", "default"])
        self.assertEqual(self.prompts, [("default", "after login")])
        self.assertEqual(path.read_text(), '{"new_login":true}')

    def test_relogin_clears_stale_exhaustion_for_the_changed_profile(self):
        self.manager.accounts.exhausted("default")
        account_state_path(self.root, "default").write_text('{"refreshed":true}')
        self.assertEqual(self.manager.send_prompt("use refreshed session"), ("reply from default", []))
        self.assertEqual(self.manager.active_account, "default")

    def test_all_exhausted_returns_a_typed_error_without_spinning(self):
        self.quotas = {name: {"exhausted": True, "resets_at": time.time() + 3600} for name in self.quotas}
        with self.assertRaises(QuotaExhaustedError):
            self.manager.send_prompt("request")
        self.assertFalse(self.prompts)
        self.assertEqual(len(self.opened), 2)
        with self.assertRaises(QuotaExhaustedError):
            self.manager.send_prompt("request again")
        self.assertEqual(len(self.opened), 2)

    def test_quota_all_does_not_change_active_account_or_send_model_prompts(self):
        status = self.manager.get_quota(all_accounts=True)
        self.assertEqual(status["active_account"], "default")
        self.assertEqual([account["name"] for account in status["accounts"]], ["default", "backup"])
        self.assertFalse(self.prompts)
        self.assertFalse(self.images)


if __name__ == "__main__":
    unittest.main()
