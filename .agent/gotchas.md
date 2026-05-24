# Common Gotchas

## Do not use `ruff format`

`CONTRIBUTING.md` mentions `ruff format`, but **do not run it** — it destroys git blame history. Only `ruff check` should be used.

## Config `${VAR}` References

`config/loader.py` resolves `${VAR}` patterns in `config.json` at load time. This is **not** a shell-like default-value syntax. If the environment variable is missing, `load_config` raises `ValueError` and the agent falls back to default configuration.

Example valid usage:
```json
{ "providers": { "openrouter": { "apiKey": "${OPENROUTER_KEY}" } } }
```

## Windows Compatibility

nanobot explicitly supports Windows. Key differences to keep in mind:
- `ExecTool` uses `cmd /c` on Windows instead of `sh -c` (`shell.py`).
- `cli/commands.py` forces `sys.stdout`/`stderr` to UTF-8 on startup to handle emoji and multilingual input.
- MCP stdio server commands are normalized for Windows path separators (`mcp.py`).
- Always use `pathlib.Path` for path manipulation; do not assume `/` separators.

## Prompt Templates

Agent system prompts and scenario-specific instructions live in `nanobot/templates/` as Jinja2 markdown files (`identity.md`, `platform_policy.md`, `HEARTBEAT.md`, `SOUL.md`, etc.). Changing these files alters agent behavior as directly as changing Python code. They are loaded by `utils/prompt_templates.py`.

Tool descriptions, skills, and replayed session history also shape model behavior. Treat changes to those surfaces like runtime code: keep them narrow, add a focused regression test when possible, and avoid teaching the model to repeat internal markers, local paths, or tool-call text.

## Context Pollution Persists

Anything written into memory, session history, or prompt inputs can be replayed into future LLM calls. Metadata such as timestamps, local media paths, tool-call echoes, and raw fallback dumps must be bounded and sanitized before they become examples for the model to imitate.

## Heartbeat Virtual Tool Call

The heartbeat service (`heartbeat/service.py`) does not parse free-text LLM output. Instead, it injects a virtual `heartbeat` tool with `action: skip | run` into the conversation. Phase 1 is a structured decision; Phase 2 executes only on `run`. When adding new periodic background checks, follow this virtual-tool-call pattern rather than string matching.

## Skills as Extension Point

Built-in skills live in `nanobot/skills/` (markdown + YAML frontmatter format). Agent capabilities that are "know-how" rather than code should be added as skills, not hardcoded into the agent loop. External skills can be published to and installed from ClawHub.

## Atomic Session Writes

`agent/memory.py` writes `history.jsonl` atomically (temp file + fsync + rename + directory fsync). This guarantees durability across crashes. Do not replace this with a plain `open(..., "w")` write.

## Session Key Format

Session keys use the format `channel:user_id:chat_id` (e.g. `websocket:abc123:def456`). When parsing keys, handle both 3-part (user-scoped) and 2-part (legacy) formats. The `splitKey()` function in `webui/src/lib/api.ts` handles this:

```ts
// 3-part: channel:user_id:chat_id
// 2-part (legacy): channel:chat_id
```

New code that constructs session keys must include the `user_id` segment (use `_session_key_for_user()` in Python, `getUser()?.id` in TypeScript).

## Auth System

- The auto-created admin password is printed to **console only** on first run. If lost, use `python3 -c "from nanobot.auth import get_user_by_username, update_user; u=get_user_by_username('admin'); update_user(u.id, password='newpass')"` to reset.
- JWT tokens are invalidated on gateway restart (per-process random signing key).
- The `websockets` library used for the HTTP server only handles GET requests. All auth endpoints use query parameters or headers rather than POST bodies.
- `_parse_basic_auth()` extracts credentials from the `Authorization: Basic <base64>` header. Empty username+password falls through to legacy localhost mode.

## IM Channels Removed

All IM platform integrations (Telegram, Discord, Slack, WhatsApp, WeChat, WeCom, DingTalk, Feishu, Matrix, MoChat, MS Teams, Signal, QQ, Email) have been removed. The only remaining channel is `websocket.py`. Channel auto-discovery via `pkgutil` means no registry changes were needed — the files were simply deleted.
