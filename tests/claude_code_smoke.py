"""Opt-in real Claude Code test; add --live to use a saved Muse web session.

Run: python tests/claude_code_smoke.py [path/to/claude]
Or:  python tests/claude_code_smoke.py npx --yes --package @anthropic-ai/claude-code claude
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from muse_bridge import BrowserManager, create_daemon_server, load_config


class ScriptedMuse:
    url = "https://muse.ai/scripted-test"

    def __init__(self, file):
        self.file = file
        self.contexts = []

    def send_prompt(self, prompt, cwd=""):
        assert cwd == "", "Bridge must leave filesystem operations to Claude Code"
        context, _ = json.JSONDecoder().raw_decode(prompt.split("CONTEXT_JSON:\n", 1)[1])
        self.contexts.append(context)
        results = [block for message in context["messages"] if isinstance(message["content"], list) for block in message["content"] if block["type"] == "tool_result"]
        assert not any(block["is_error"] for block in results), "Client tool failed: " + json.dumps(results)
        calls = [
            {"name": "Read", "input": {"file_path": str(self.file)}},
            {"name": "Edit", "input": {"file_path": str(self.file), "old_string": 'print("old")', "new_string": 'print("xin chào")'}},
            {"name": "Bash", "input": {"command": shlex.quote(sys.executable) + " hello.py", "description": "Run the temporary hello script"}},
        ]
        if len(results) < len(calls):
            response = {"request_id": prompt.splitlines()[0].split()[-1], "text": "", "tool_calls": [calls[len(results)]]}
        else:
            assert "xin chào" in json.dumps(results[-1], ensure_ascii=False)
            response = {"request_id": prompt.splitlines()[0].split()[-1], "text": "SMOKE_OK", "tool_calls": []}
        return json.dumps(response, ensure_ascii=False), []


def main():
    arguments = sys.argv[1:]
    live = bool(arguments and arguments[0] == "--live")
    command = (arguments[1:] if live else arguments) or ["claude"]
    version = subprocess.run(command + ["--version"], capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="muse-claude-smoke-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        file = project / "hello.py"
        file.write_text('print("old")\n')
        if live:
            config = load_config()
            muse = BrowserManager(config["chat_url"], headless=False)
            muse.start()
        else:
            muse = ScriptedMuse(file)
        server = create_daemon_server(muse, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
        env.update({
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ANTHROPIC_API_KEY": "muse-scripted-test",
            "CLAUDE_CONFIG_DIR": str(root / "config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        try:
            process = subprocess.run(command + [
                "-p", f"Dùng tool Read đọc hello.py trong thư mục làm việc hiện tại. Dùng Edit sửa để file in chính xác xin chào. Dùng Bash chạy bằng {sys.executable}. Chỉ trả lời SMOKE_OK sau khi chạy thành công. Mọi thao tác phải qua client tools, không dùng workspace riêng trên web.",
                "--bare", "--model", "claude-sonnet-4-6", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--allowedTools", "Read,Edit,Bash",
            ], cwd=project, env=env, capture_output=True, text=True, timeout=600 if live else 60)
            if process.returncode:
                raise RuntimeError(process.stdout + process.stderr)
            events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
            result = next(event for event in reversed(events) if event.get("type") == "result")
            tool_names = [block["name"] for event in events if event.get("type") == "assistant" for block in event["message"]["content"] if block.get("type") == "tool_use"]
            assert result.get("result") == "SMOKE_OK", result
            assert file.read_text() == 'print("xin chào")\n'
            assert tool_names == ["Read", "Edit", "Bash"], tool_names
            if not live:
                assert len(muse.contexts) >= 4
            print(f"PASS: {version}; backend={'LIVE Muse web' if live else 'scripted Muse'}: {' -> '.join(tool_names)} -> SMOKE_OK; temporary workspace changed successfully.")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            if live:
                muse.stop()


if __name__ == "__main__":
    main()
