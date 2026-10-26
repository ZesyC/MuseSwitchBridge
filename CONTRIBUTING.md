# Contributing

Keep changes focused and describe the behavior they change. Bugs involving Muse's interface should include steps to reproduce and relevant selector details, with private account and conversation data removed.

## Local checks

Install the dependencies and Playwright Chromium as described in the [README](README.md), then run:

```bash
python -m unittest discover -s tests -v
git diff --check
```

The suite uses a scripted Muse backend and local browser fixtures. It covers Messages conversion, schema validation, client workspace tools, SSE, image uploads, cookie isolation and account selection. It does not require a Muse login or spend account quota.

## Claude Code smoke test

With an installed `claude` command:

```bash
python tests/claude_code_smoke.py
```

If the binary is not on PATH, pass its location:

```bash
python tests/claude_code_smoke.py /path/to/claude
```

This runs the real Claude Code CLI against a scripted backend, using isolated Claude settings and a temporary project. It checks a `Read` → `Edit` → `Bash` workflow and the resulting file contents.

## Opt-in live checks

After saving a Muse login, run:

```bash
python tests/claude_code_smoke.py --live /path/to/claude
python tests/image_smoke.py
```

These checks send requests to Muse and consume account quota. The coding check uses a temporary local project; the image check uploads a generated image and verifies a follow-up using the image in conversation history. Test messages remain in the Muse chat.

Use a fresh Muse chat for live testing. Account rotation tests use simulated quota states; testing a live switch requires at least two authenticated profiles.

## Code layout

| Path | Responsibility |
| --- | --- |
| `muse_bridge.py` | CLI, browser automation, worker and HTTP server |
| `muse_protocol.py` | Messages conversion, images, schema validation and SSE |
| `muse_accounts.py` | Saved profiles, subscription parsing and quota selection |
| `tests/` | Automated checks and opt-in live tests |

Never include session files, local configuration, credentials, screenshots of private chats or unredacted request dumps in a commit or issue. The `.gitignore` excludes runtime sessions and diagnostics; review your staged files before committing.
