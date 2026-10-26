# Reference

[Back to the README](../README.md)

## Claude Code CLI

Keep the bridge server running. In the terminal where you launch Claude Code, set:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8765"
export ANTHROPIC_AUTH_TOKEN="muse-bridge-local"
export ANTHROPIC_API_KEY="muse-bridge-local"
cd /path/to/your/project
claude
```

PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8765"
$env:ANTHROPIC_AUTH_TOKEN="muse-bridge-local"
$env:ANTHROPIC_API_KEY="muse-bridge-local"
cd C:\path\to\your\project
claude
```

These credentials are placeholders for the local client. MuseBridge does not validate them or use them to log in to Muse.

## API routes

The server binds to `127.0.0.1:8765` by default.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | Server status, current chat URL and active account |
| GET | `/accounts` | Saved profiles, cached quota and selected quota phase |
| GET | `/quota` | Refresh usage for the browser's current account |
| GET | `/quota?all=1` | Refresh usage for every saved account |
| GET | `/v1/models` | Compatibility model labels, not Muse model selection |
| POST | `/v1/messages` | Text, base64 images and client tool conversations |
| POST | `/v1/messages/count_tokens` | Approximate input token count |
| POST | `/v1/chat/completions` | Legacy text chat; no agent tool protocol |
| POST | `/send` | Raw prompt; may download and extract Muse archives into the server's working directory |
| GET | `/read?limit=5` | Recent messages in the current browser chat |
| POST | `/chat_url` | Navigate to the URL in `{"url":"https://muse.ai/thread/new"}` |
| POST | `/reset` | Navigate the current browser to the Muse home page |

Use `/v1/messages` for coding agents. That endpoint leaves filesystem operations to the client's tools and does not extract Muse archives onto disk.

### Messages example

```bash
curl http://127.0.0.1:8765/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"muse","max_tokens":1024,"messages":[{"role":"user","content":"Say hello."}]}'
```

`model` and `max_tokens` provide client compatibility; they do not switch Muse's model or enforce a web generation limit.

## Configuration

`login` creates `bridge_config.json` beside the Python files. It is private and ignored by Git. Use [bridge_config.example.json](../bridge_config.example.json) as a reference; copying it is optional.

| Setting | Purpose |
| --- | --- |
| `chat_url` | Starting chat for the default profile; saved during login |
| `port` | Configuration value for the local server port |
| `headless` | Whether the managed browser is hidden |
| `proxy` | Proxy URL used by the browser |
| `cdp_url` | Optional external Chromium debugging endpoint; single-account mode only |
| `locale`, `timezone_id`, `geolocation` | Browser context settings |
| `selectors` | Discovered composer, send-button and message selectors |

CLI flags explicitly choose the port, for example `serve --port 9000` and `quota --port 9000`. If you change the port, also update Claude Code's base URL. Use flags rather than relying on the configuration value: several commands have a CLI default of `8765`.

Raw CLI sends accept `send --timeout SECONDS` (default: 600). Messages API requests also use a 600-second browser timeout by default; `max_tokens` does not change that timeout.

Use `serve --headful` to show the browser. Multiple account profiles require isolated browser contexts and cannot share `cdp_url`.

### Saved profiles

| Profile | Local file |
| --- | --- |
| `default` | `storage_state.json` |
| Named account | `.muse_accounts/NAME.json` |

Names accept 1–64 ASCII letters, digits, underscores and hyphens. Sessions are written atomically, with owner-only permissions on POSIX systems. Re-running login for a profile replaces its saved session; the daemon detects the new login on a subsequent request.

Quota responses include `quota_phase` (`weekly` or `additional`), the active account and each account's last quota reading. `checked_at` and `resets_at` use Unix seconds. Human-readable reset timestamps are labeled UTC.

An account confirmed completely exhausted is skipped until its known reset time. Run `quota --all` to force a refresh sooner, for example after adding credits. If no reset is known, a manual refresh or new login is needed to reconsider that account.

## Tool protocol

For each Messages request, MuseBridge sends the system instructions, conversation, tool schemas and image references to Muse. Each request has a unique ID. Muse is asked to return a fenced JSON envelope:

```json
{
  "request_id": "ID supplied by the bridge",
  "text": "Reading the project README.",
  "tool_calls": [
    {"name": "Read", "input": {"file_path": "/path/to/project/README.md"}}
  ]
}
```

The bridge validates the request ID, tool names and arguments against the client's JSON Schemas. It translates valid calls into Anthropic `tool_use` blocks. Claude Code executes them and returns `tool_result` blocks on the next turn. The bridge preserves IDs and error results, including MCP tools supplied by the client.

For a final answer, `tool_calls` is empty. A malformed response gets one correction attempt. A second invalid response fails the request; partial groups of tool calls are not emitted.

Images are uploaded as real browser attachments. Base64 data is excluded from prompt text. Images in conversation history are uploaded again on follow-up turns, with duplicates removed within each request.

## Responses and failures

- Streaming requests receive heartbeat events while Muse works. Complete validated blocks are then sent as Anthropic SSE events, including `input_json_delta` for tool inputs.
- Failures after streaming begins use an SSE `error` event. Non-streaming quota exhaustion returns HTTP 429 with `rate_limit_error`.
- An active generation is not considered complete just because its text pauses. Timed-out partial output is discarded.
- Quota rejection may resend the pending prompt and its attachments to another account. Completed client tool calls are not rerun by the bridge.
- Network failures, invalid model JSON and expired sessions do not trigger quota-based account failover.
- Session-title and suggestion requests are handled locally to avoid unnecessary Muse generations.

Extended thinking and Anthropic server-side tools are not emulated. Prior assistant thinking blocks are omitted. Full history is sent without silently truncating context, so long conversations may exceed what Muse web accepts.

## Other interfaces

`send` and `read` can use the running daemon or open a one-shot browser when no daemon is available. One-shot mode uses the default saved session and does not rotate accounts.

```bash
python muse_bridge.py send "Explain binary search"
python muse_bridge.py read --limit 5
```

The optional `mcp` command exposes `ask_muse` and `read_muse_chat` over stdio. It is a separate interface for asking Muse questions, not the Messages adapter that powers Claude Code's workspace tools. `launch-chrome` is a Windows helper for external Chrome/Edge debugging.
