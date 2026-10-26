# MuseBridge

Use your Muse web session from Claude Code, including the VS Code extension.

MuseBridge runs a local API that connects Claude Code to Muse through a browser. Muse decides what to do; Claude Code reads files, edits code and runs commands in your project using its own tools and permissions.

```text
Claude Code  ↔  MuseBridge on localhost  ↔  Muse web
     ↕
Your project files and terminal
```

This is an unofficial browser adapter. It uses a saved Muse login, not a Muse API key, and does not provide the Claude models named by a client's compatibility settings.

## What it supports

- **Workspace tools:** preserves tool definitions, conversation history, tool results and errors across turns.
- **Images:** uploads pasted screenshots and supported image blocks as real Muse attachments.
- **Multiple accounts:** keeps separate login sessions and switches accounts as quota runs out.
- **Weekly quota first:** uses every account's weekly allowance before moving to additional tokens.
- **Usage checks:** reads account usage, additional-token labels and reset times from Muse Settings.
- **Claude Code compatibility:** Anthropic Messages responses, validated tool calls and buffered SSE responses.

## Get running

You need Python 3.10+, access to Muse web, and either the Claude Code extension or CLI.

### Install

Run these commands from the repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

On Windows, create and activate the environment with:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Use the activated environment for the commands below.

### Save a login

```bash
python muse_bridge.py login
```

A Chromium window opens. Sign in to Muse, open a chat, then return to the terminal and press Enter. MuseBridge saves the browser session and discovers the chat controls automatically.

The login window closes after saving. You do not need to keep it open.

### Start the server

```bash
python muse_bridge.py serve
```

Keep this terminal running. The API is available at `http://127.0.0.1:8765`. The server manages its own browser in the background.

On Windows, `start_muse_bridge.bat` also starts the server and uses the repository's `.venv` when available.

### Connect the VS Code extension

Open **Preferences: Open User Settings (JSON)** in VS Code and merge the following into your settings:

```json
{
  "claudeCode.environmentVariables": [
    { "name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:8765" },
    { "name": "ANTHROPIC_AUTH_TOKEN", "value": "muse-bridge-local" },
    { "name": "ANTHROPIC_API_KEY", "value": "muse-bridge-local" }
  ],
  "claudeCode.disableLoginPrompt": true,
  "claudeCode.useTerminal": false
}
```

The key and token above are local placeholders. Your saved browser session authenticates with Muse.

Open your project folder and start a new conversation in the Claude Code panel. If the panel still uses its previous settings, run **Developer: Reload Window**.

Try:

> Read README.md in this project and summarize what it does.

You should see Claude Code use its `Read` tool. To check editing and execution:

> Create hello.py that prints "hello", run it with Python, and report the output.

A successful task should show actual local tool results.

Prefer the terminal? See [Claude Code CLI setup](docs/REFERENCE.md#claude-code-cli).

## Accounts and quota

Your first login is named `default`. Add more accounts with:

```bash
python muse_bridge.py login --account account01
python muse_bridge.py login --account account02
```

Each login opens a separate browser session. Sign in to the intended account and press Enter in the terminal to save it. New profiles are discovered without restarting the server.

Inspect accounts and usage:

```bash
python muse_bridge.py accounts
python muse_bridge.py quota --all
python muse_bridge.py quota --all --json
```

The selection order is:

```text
Weekly:      default → account01 → account02
Additional:  default → account01 → account02
```

Each account is used until its current allowance is exhausted. Account order is `default` first, then named profiles alphabetically.

- At 100% weekly usage, the next account with weekly quota is selected.
- Additional tokens are used only after all saved accounts' weekly allowances are confirmed exhausted.
- When weekly quota resets, it takes priority again before the next request.
- If a weekly quota lookup fails, another confirmed weekly allowance can still be used. Additional spending waits until all weekly allowances can be checked.
- When all accounts are exhausted, the API returns a quota error instead of looping.

Usage labels come from Muse's **Settings → General → Usage**. Additional-token balances are displayed as Muse reports them; raw provider balance units are not treated as token counts.

**Switching happens between requests.** If a generation crosses the weekly limit while it is running, Muse may consume additional tokens before the bridge can switch accounts.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `python muse_bridge.py serve` | Start the local API |
| `python muse_bridge.py serve --headful` | Start with a visible browser |
| `python muse_bridge.py login --account NAME` | Save or renew an account login |
| `python muse_bridge.py accounts` | List saved profiles |
| `python muse_bridge.py quota --all` | Refresh usage for every account |
| `python muse_bridge.py inspect` | Diagnose chat controls and save a debug screenshot |
| `python muse_bridge.py --help` | List all commands |

The [reference](docs/REFERENCE.md) covers API routes, configuration and protocol behavior.

## Troubleshooting

| Problem | Check |
| --- | --- |
| Claude Code asks for a login | Merge all three environment variables into VS Code settings, reload the window and open a new conversation. |
| Cannot connect to the API | Keep `serve` running and open `http://127.0.0.1:8765/health`. |
| Muse replies but no files change | Check for actual Claude Code tool calls; restart the bridge after code updates and use a fresh Muse chat if it has conflicting instructions. |
| Login expired or chat will not open | Run `login` again for the affected account. |
| Weekly quota is unavailable | Retry `quota --all`; inspect Muse Settings or use `serve --headful` to check the page. |
| Chat controls are no longer detected | Run `inspect` and review the selectors in `bridge_config.json`. |

## Boundaries

Tool calling is driven by a response protocol in the prompt. It depends on Muse following that protocol and on its web interface remaining compatible.

- SSE responses are buffered until a complete answer has been validated; they are not a live token stream from Muse.
- Model names are compatibility labels. API controls such as `max_tokens`, sampling and stop sequences cannot be enforced through the web UI.
- Images support base64 PNG, JPEG, GIF and WebP: up to 10 MiB per image and 20 MiB per HTTP request. PDFs, URL images and file-ID images are unsupported.
- Token counts are estimates. Account rotation does not add quota to any account.
- Browser requests run sequentially; this is a local development bridge.

File contents, tool output and images included in a request are sent to Muse. Keep the unauthenticated server on loopback. Never publish `storage_state.json`, `.muse_accounts/` or `bridge_config.json`; these are ignored by Git.

## Development

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and opt-in tests with real Claude Code and Muse sessions.

MuseBridge is not affiliated with Muse or Anthropic.
