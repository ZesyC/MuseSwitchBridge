import base64
import copy
import json
import subprocess
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from collections import deque
from pathlib import Path
from unittest.mock import patch

import muse_bridge
from muse_accounts import QuotaExhaustedError
from muse_protocol import ProtocolError, build_prompt, estimate_input_tokens, parse_reply


def png_bytes(color=(255, 0, 0)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color))) + chunk(b"IEND", b"")


def image_block(data=None):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(data or png_bytes()).decode()}}


TOOLS = [
    {"name": "Read", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"], "additionalProperties": False}},
    {"name": "Edit", "input_schema": {"type": "object", "properties": {key: {"type": "string"} for key in ("file_path", "old_string", "new_string")}, "required": ["file_path", "old_string", "new_string"]}},
    {"name": "Write", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}},
    {"name": "Bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "mcp__repo__search", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def request():
    return {"model": "muse", "system": [{"type": "text", "text": "Working directory: /client/project\nFollow CLAUDE.md", "cache_control": {"type": "ephemeral"}}], "tools": copy.deepcopy(TOOLS), "messages": [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>Project rules</system-reminder>\nĐọc file rồi sửa và chạy kiểm tra."}]}]}


def envelope(calls=None, text="", request_id="turn"):
    return json.dumps({"request_id": request_id, "text": text, "tool_calls": calls or []}, ensure_ascii=False)


class ProtocolTests(unittest.TestCase):
    def test_assistant_prefill_is_preserved_and_system_only_is_rejected(self):
        payload = request()
        payload["messages"].append({"role": "assistant", "content": "Let me inspect the file."})
        prompt = build_prompt(payload, "turn")
        self.assertIn("Let me inspect the file.", prompt)
        payload["messages"] = [{"role": "system", "content": "No user task"}]
        with self.assertRaisesRegex(ValueError, "user message"):
            build_prompt(payload, "turn")

    def test_extension_system_messages_preserve_workspace_instructions(self):
        payload = request()
        system = {"role": "system", "content": [{"type": "text", "text": "Working directory: /extension/workspace\nRead CLAUDE.md"}]}
        payload["messages"].insert(0, system)
        payload["messages"].append({"role": "system", "content": "Active editor context at the end of the request"})
        context, _ = json.JSONDecoder().raw_decode(build_prompt(payload, "turn").split("CONTEXT_JSON:\n", 1)[1])
        self.assertEqual(context["messages"][0], system)
        self.assertEqual(context["messages"][-1], payload["messages"][-1])
        self.assertEqual(context["messages"][-2]["role"], "user")

    def test_preserves_system_reminders_history_ids_and_errors(self):
        payload = request()
        payload["messages"] += [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "private", "signature": "opaque"}, {"type": "tool_use", "id": "call1", "name": "Read", "input": {"file_path": "/client/project/a.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "is_error": True, "content": [{"type": "text", "text": "Permission denied"}]}]},
        ]
        prompt = build_prompt(payload, "turn")
        context, _ = json.JSONDecoder().raw_decode(prompt.split("CONTEXT_JSON:\n", 1)[1])
        self.assertEqual(context["system"][0]["text"], payload["system"][0]["text"])
        self.assertIn("<system-reminder>", context["messages"][0]["content"][0]["text"])
        self.assertEqual(context["tools"], TOOLS)
        self.assertEqual(context["messages"][-1], payload["messages"][-1])
        self.assertEqual(context["messages"][-2]["content"][0]["id"], "call1")
        self.assertNotIn("opaque", prompt)

    def test_complete_nested_json_unicode_and_code_survive_dom_wrappers(self):
        code = 'print("xin chào")\nconfig = {"items": [{"value": "```"}]}\n'
        call = {"name": "Write", "input": {"file_path": "/client/project/a.py", "content": code}}
        for wrapper in ("{}", "```json\n{}\n```", "json\nCopy code\n{}\n"):
            with self.subTest(wrapper=wrapper):
                blocks = parse_reply(wrapper.format(envelope([call], "Đang ghi")), request(), "turn")
                self.assertEqual(blocks[1]["input"]["content"], code)
                self.assertTrue(blocks[1]["id"].startswith("toolu_"))

    def test_rejects_prose_stale_truncated_and_ambiguous_responses(self):
        for reply in ("Đã sửa xong", envelope(request_id="old"), envelope()[:-2], envelope() + envelope()):
            with self.subTest(reply=reply), self.assertRaises(ProtocolError):
                parse_reply(reply, request(), "turn")

    def test_unknown_tools_and_invalid_schema_inputs_are_rejected_atomically(self):
        good = {"name": "Read", "input": {"file_path": "a.py"}}
        bad_calls = [
            {"name": "unknown", "input": {}},
            {"name": "Read", "input": {}},
            {"name": "Read", "input": {"file_path": 123}},
            {"name": "Read", "input": {"file_path": "a.py", "extra": True}},
            {"name": "Read", "input": "{\"file_path\":\"a.py\"}"},
        ]
        for bad in bad_calls:
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                parse_reply(envelope([good, bad]), request(), "turn")

    def test_duplicate_keys_nonfinite_values_and_empty_answers_are_rejected(self):
        replies = [
            '{"request_id":"turn","text":"","tool_calls":[],"tool_calls":[{"name":"Bash","input":{"command":"echo x"}}]}',
            '{"request_id":"turn","text":"","tool_calls":[{"name":"Read","input":{"file_path":"x","number":NaN}}]}',
            envelope(),
        ]
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(ProtocolError):
                parse_reply(reply, request(), "turn")

    def test_tool_choice_modes_and_parallel_restriction(self):
        calls = [{"name": "Read", "input": {"file_path": "a.py"}}, {"name": "mcp__repo__search", "input": {"query": "bug"}}]
        for choice, selected in [({"type": "none"}, calls), ({"type": "any"}, []), ({"type": "tool", "name": "Edit"}, calls), ({"type": "auto", "disable_parallel_tool_use": True}, calls)]:
            payload = request()
            payload["tool_choice"] = choice
            with self.subTest(choice=choice), self.assertRaises(ProtocolError):
                parse_reply(envelope(selected), payload, "turn")
        payload = request()
        payload["tool_choice"] = {"type": "tool", "name": "Read"}
        self.assertEqual(parse_reply(envelope(calls[:1]), payload, "turn")[0]["name"], "Read")
        self.assertEqual(len(parse_reply(envelope(calls), request(), "turn")), 2)

    def test_local_refs_and_complex_schema_constraints(self):
        payload = request()
        payload["tools"] = [{"name": "structured", "input_schema": {"type": "object", "$defs": {"item": {"type": "integer", "minimum": 1}}, "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/item"}, "minItems": 1}}, "required": ["items"]}}]
        build_prompt(payload, "turn")
        self.assertEqual(parse_reply(envelope([{"name": "structured", "input": {"items": [1, 2]}}]), payload, "turn")[0]["input"], {"items": [1, 2]})
        with self.assertRaises(ProtocolError):
            parse_reply(envelope([{"name": "structured", "input": {"items": [0]}}]), payload, "turn")

    def test_remote_refs_never_fetch_network(self):
        payload = request()
        payload["tools"][0]["input_schema"] = {"$ref": "https://example.invalid/schema"}
        with patch("urllib.request.urlopen", side_effect=AssertionError("network access")) as network:
            with self.assertRaises(ProtocolError):
                parse_reply(envelope([{"name": "Read", "input": {"file_path": "x"}}]), payload, "turn")
            network.assert_not_called()

    def test_bad_requests_and_documents_fail_explicitly(self):
        for field, value in (("stream", "false"), ("model", {}), ("messages", []), ("tools", {}), ("tool_choice", {"type": "tool", "name": "missing"})):
            payload = request()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_prompt(payload, "turn")
        payload = request()
        payload["messages"][0]["content"] = [{"type": "document", "source": {"type": "base64", "data": "secret"}}]
        with self.assertRaisesRegex(ValueError, "Unsupported content block"):
            build_prompt(payload, "turn")

    def test_images_are_real_attachments_and_never_base64_prompt_text(self):
        payload = request()
        block = image_block()
        payload["messages"][0]["content"].insert(0, block)
        payload["system"].append(block)
        attachments = []
        prompt = build_prompt(payload, "turn", attachments)
        context, _ = json.JSONDecoder().raw_decode(prompt.split("CONTEXT_JSON:\n", 1)[1])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["buffer"], png_bytes())
        self.assertEqual(attachments[0]["mimeType"], "image/png")
        self.assertEqual(context["messages"][0]["content"][0]["attachment"], attachments[0]["name"])
        self.assertEqual(context["system"][-1]["attachment"], attachments[0]["name"])
        self.assertNotIn(block["source"]["data"], prompt)
        self.assertGreater(estimate_input_tokens(payload), estimate_input_tokens(request()) + 1500)

    def test_old_images_and_nested_tool_result_images_are_preserved_and_deduplicated(self):
        payload = request()
        payload["messages"][0]["content"].append(image_block())
        payload["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "read1", "name": "Read", "input": {"file_path": "screenshot.png"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read1", "content": [image_block(), image_block(png_bytes((0, 0, 255)))]}, {"type": "text", "text": "Compare these images"}]},
        ]
        attachments = []
        prompt = build_prompt(payload, "turn", attachments)
        context, _ = json.JSONDecoder().raw_decode(prompt.split("CONTEXT_JSON:\n", 1)[1])
        self.assertEqual(len(attachments), 2)
        first = context["messages"][0]["content"][-1]["attachment"]
        result = context["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "read1")
        self.assertEqual(result["content"][0]["attachment"], first)
        self.assertNotEqual(result["content"][1]["attachment"], first)

    def test_malformed_image_sources_fail_before_browser_upload(self):
        sources = [
            {"type": "url", "url": "http://127.0.0.1/private"},
            {"type": "base64", "media_type": "image/png", "data": "!not base64!"},
            {"type": "base64", "media_type": "image/png", "data": ""},
            {"type": "base64", "media_type": "image/svg+xml", "data": "PHN2Zz4="},
            {"type": "base64", "media_type": "image/jpeg", "data": image_block()["source"]["data"]},
        ]
        for source in sources:
            payload = request()
            payload["messages"][0]["content"].append({"type": "image", "source": source})
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_prompt(payload, "turn", [])
        payload = request()
        payload["messages"][0]["content"].append(image_block())
        with patch("muse_protocol.MAX_IMAGE_BYTES", 8), self.assertRaisesRegex(ValueError, "exceeds"):
            build_prompt(payload, "turn", [])


class FakeMuse:
    url = "https://muse.ai/test"

    def __init__(self):
        self.replies = deque()
        self.requests = []
        self.attachments = []

    def send_prompt(self, prompt, cwd="", attachments=None):
        context, _ = json.JSONDecoder().raw_decode(prompt.split("CONTEXT_JSON:\n", 1)[1])
        self.requests.append((context, cwd))
        self.attachments.append(attachments or [])
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return reply, []
        request_id = prompt.splitlines()[0].split()[-1]
        return envelope(reply.get("tool_calls"), reply.get("text", ""), request_id), []


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.muse = FakeMuse()
        self.server = muse_bridge.create_daemon_server(self.muse, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, payload, path="/v1/messages"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode(), exc.headers

    def test_read_edit_execute_roundtrip_changes_only_client_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "hello.py"
            file.write_text('print("old")\n')
            payload = request()
            payload["system"][0]["text"] = f"Working directory: {directory}"
            expected = [
                {"name": "Read", "input": {"file_path": str(file)}},
                {"name": "Edit", "input": {"file_path": str(file), "old_string": 'print("old")', "new_string": 'print("xin chào")'}},
                {"name": "Bash", "input": {"command": "python hello.py"}},
            ]
            self.muse.replies.extend([{"tool_calls": [call]} for call in expected] + [{"text": "Đã sửa và chạy thành công."}])
            for expected_call in expected:
                status, body, _ = self.post(payload)
                self.assertEqual(status, 200, body)
                response = json.loads(body)
                self.assertEqual(response["stop_reason"], "tool_use")
                call = response["content"][0]
                self.assertEqual(call["name"], expected_call["name"])
                self.assertEqual(call["input"], expected_call["input"])
                if call["name"] == "Read":
                    output = file.read_text()
                elif call["name"] == "Edit":
                    args = call["input"]
                    file.write_text(file.read_text().replace(args["old_string"], args["new_string"]))
                    output = "File edited successfully"
                else:
                    process = subprocess.run([sys.executable, str(file)], capture_output=True, text=True, check=True)
                    output = process.stdout
                payload["messages"] += [{"role": "assistant", "content": response["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": output}]}]
            status, body, _ = self.post(payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["stop_reason"], "end_turn")
            self.assertEqual(file.read_text(), 'print("xin chào")\n')
            self.assertIn("xin chào", self.muse.requests[-1][0]["messages"][-1]["content"][0]["content"])
            self.assertTrue(all(cwd == "" for _, cwd in self.muse.requests))

    def test_extension_request_with_system_role_is_accepted(self):
        payload = request()
        system = {"role": "system", "content": "Workspace instructions from VS Code"}
        payload["messages"].insert(0, system)
        payload["messages"].append(system)
        self.muse.replies.append({"tool_calls": [{"name": "Read", "input": {"file_path": "/extension/workspace/README.md"}}]})
        status, body, _ = self.post(payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["stop_reason"], "tool_use")
        self.assertEqual(self.muse.requests[0][0]["messages"][0], system)

    def test_extension_image_is_uploaded_again_after_a_tool_result(self):
        payload = request()
        payload["messages"][0]["content"].append(image_block())
        self.muse.replies.extend([{"tool_calls": [{"name": "Read", "input": {"file_path": "package.json"}}]}, {"text": "Đã xem ảnh và dependencies."}])
        status, body, _ = self.post(payload)
        self.assertEqual(status, 200, body)
        response = json.loads(body)
        call = response["content"][0]
        payload["messages"] += [{"role": "assistant", "content": response["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "{}"}]}]
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual([len(items) for items in self.muse.attachments], [1, 1])
        self.assertEqual(self.muse.attachments[0][0]["buffer"], png_bytes())
        self.assertEqual(self.muse.attachments[0][0]["name"], self.muse.attachments[1][0]["name"])

    def test_image_token_count_is_an_estimate_without_a_browser_call(self):
        payload = request()
        payload["messages"][0]["content"].append(image_block())
        status, body, _ = self.post(payload, "/v1/messages/count_tokens")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["input_tokens"], estimate_input_tokens(payload))
        self.assertFalse(self.muse.requests)

    def test_bad_image_returns_400_without_any_model_call(self):
        payload = request()
        payload["stream"] = True
        payload["messages"][0]["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "invalid"}})
        status, body, _ = self.post(payload)
        self.assertEqual(status, 400, body)
        self.assertIn("base64", json.loads(body)["error"]["message"])
        self.assertFalse(self.muse.requests)

    def test_failed_write_result_reaches_model_and_can_trigger_recovery(self):
        payload = request()
        payload["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "write1", "name": "Write", "input": {"file_path": "x", "content": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write1", "is_error": True, "content": "Permission denied"}, {"type": "text", "text": "Hãy kiểm tra quyền trước"}]},
        ]
        self.muse.replies.append({"tool_calls": [{"name": "Bash", "input": {"command": "pwd"}}]})
        status, body, _ = self.post(payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["stop_reason"], "tool_use")
        self.assertEqual(self.muse.requests[0][0]["messages"][-1], payload["messages"][-1])

    def test_parallel_mcp_calls_and_results_keep_ids(self):
        payload = request()
        calls = [{"name": "Read", "input": {"file_path": "a.py"}}, {"name": "mcp__repo__search", "input": {"query": "bug"}}]
        self.muse.replies.extend([{"tool_calls": calls}, {"text": "Đã kiểm tra."}])
        _, body, _ = self.post(payload)
        blocks = json.loads(body)["content"]
        self.assertNotEqual(blocks[0]["id"], blocks[1]["id"])
        results = [{"type": "tool_result", "tool_use_id": b["id"], "content": [{"type": "text", "text": b["name"]}]} for b in reversed(blocks)]
        payload["messages"] += [{"role": "assistant", "content": blocks}, {"role": "user", "content": results}]
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual(self.muse.requests[-1][0]["messages"][-1]["content"], [{**r, "is_error": False} for r in results])

    def test_invalid_output_retries_once_without_executing_anything(self):
        self.muse.replies.extend(["Tôi sẽ đọc file", {"tool_calls": [{"name": "Read", "input": {"file_path": "a.py"}}]}])
        status, body, _ = self.post(request())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["content"][0]["name"], "Read")
        self.assertEqual(len(self.muse.requests), 2)
        self.muse.replies.extend(["invalid", "invalid"])
        status, body, _ = self.post(request())
        self.assertEqual(status, 502, body)
        self.assertEqual(json.loads(body)["type"], "error")

    def test_stream_can_be_reassembled_into_native_tool_blocks(self):
        payload = request()
        payload["stream"] = True
        args = {"file_path": "a.py", "content": 'print("xin chào")\n'}
        self.muse.replies.append({"text": "Đang tạo", "tool_calls": [{"name": "Write", "input": args}]})
        status, body, headers = self.post(payload)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([e["index"] for e in starts], [0, 1])
        self.assertEqual(starts[1]["content_block"]["input"], {})
        delta = next(e["delta"] for e in events if e.get("delta", {}).get("type") == "input_json_delta")
        self.assertEqual(json.loads(delta["partial_json"]), args)
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")

    def test_stream_failure_is_an_error_event_without_second_http_response(self):
        payload = request()
        payload["stream"] = True
        self.muse.replies.append(RuntimeError("Muse disconnected"))
        status, body, _ = self.post(payload)
        self.assertEqual(status, 200)
        self.assertIn('event: error\n', body)
        self.assertIn("Muse disconnected", body)
        self.assertNotIn("HTTP/1.", body)
        self.assertNotIn("event: message_stop", body)

    def test_all_accounts_exhausted_returns_native_rate_limit_error(self):
        self.muse.replies.append(QuotaExhaustedError("All saved accounts are out of quota"))
        status, body, _ = self.post(request())
        self.assertEqual(status, 429, body)
        self.assertEqual(json.loads(body)["error"]["type"], "rate_limit_error")
        self.assertEqual(len(self.muse.requests), 1)

    def test_bad_http_input_is_400_without_calling_muse(self):
        for body in (b"[1]", b"{broken", b"\xff", b"{}"):
            with self.subTest(body=body):
                status, response, _ = self.post(body)
                self.assertEqual(status, 400, response)
                self.assertEqual(json.loads(response)["type"], "error")
        self.assertFalse(self.muse.requests)

    def test_background_title_suggestion_and_token_count_do_not_call_muse(self):
        for text in ("[SUGGESTION MODE: predict]", "Generate a session title for this task"):
            payload = request()
            payload["messages"] = [{"role": "user", "content": text}]
            self.assertEqual(self.post(payload)[0], 200)
        small = json.loads(self.post(request(), "/v1/messages/count_tokens")[1])["input_tokens"]
        payload = request()
        payload["messages"][0]["content"][0]["text"] += "long input " * 1000
        large = json.loads(self.post(payload, "/v1/messages/count_tokens")[1])["input_tokens"]
        self.assertGreater(large, small)
        self.assertFalse(self.muse.requests)


class BrowserCompletionTests(unittest.TestCase):
    def test_generation_pause_does_not_return_partial_tool_arguments(self):
        class Page:
            def evaluate(self, script):
                pass

        messages = [{"author": "user", "text": "prompt"}, {"author": "assistant", "text": '{"partial": true}'}]
        with patch.object(muse_bridge, "_extract_messages", return_value=messages), patch.object(muse_bridge, "_is_generating", side_effect=[True] * 25 + [False]) as generating, patch.object(muse_bridge.time, "sleep"):
            reply = muse_bridge._wait_for_reply(Page(), "x", timeout_s=5, prompt_text="prompt")
        self.assertEqual(generating.call_count, 26)
        self.assertEqual(reply, '{"partial": true}')

    def test_timeout_discards_partial_output(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete output was discarded"):
            muse_bridge._wait_for_reply(None, "x", timeout_s=0)


if __name__ == "__main__":
    unittest.main()
