# Security Boundaries

The agent operates with significant power (file system, shell, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`) resolve paths through `_resolve_path` (`agent/tools/filesystem.py`), which enforces that the resolved path must lie under `allowed_dir` (typically the configured workspace), plus the media upload directory (`get_media_dir()`) and any `extra_allowed_dirs`.

Shell execution (`ExecTool`, `agent/tools/shell.py`) also respects `restrict_to_workspace`: if enabled and `working_dir` is outside the workspace, the command is rejected before execution.

**Rule**: Any new path-handling logic must go through `_resolve_path` or perform an equivalent `allowed_dir` check.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through `validate_url_target` (`security/network.py`). By default it blocks RFC1918 private addresses, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

The only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

## Shell Sandbox

`tools/sandbox.py` provides optional command wrapping. The only backend currently shipped is `bwrap` (bubblewrap), intended for containerized deployments. On Windows and bare-metal Linux without `bwrap`, commands run in the native shell with workspace restriction as the only guard.

**Rule**: If adding a new sandbox backend, implement `_wrap_<name>(command, workspace, cwd) -> str` and register it in `_BACKENDS`.

## Authentication & User Isolation

The auth system (`nanobot/auth/`) manages users in a local SQLite database (`~/.nanobot/auth.db`).

- **Password hashing**: bcrypt via `auth/password.py`. Never store or compare plaintext passwords.
- **Tokens**: JWT (HS256) via `auth/tokens.py`. The signing key is a per-process random secret (regenerated on restart, invalidating all existing tokens). Tokens expire after 24 hours by default.
- **Default admin**: On first run, `auth/db.py` auto-creates an `admin` user with a random 16-character password printed to the console. Change this password immediately.

**WebSocket channel auth flow** (`websocket.py`):

- `/webui/bootstrap` accepts Basic Auth (`username:password`) and returns a JWT + a short-lived WS handshake token.
- All `/api/*` endpoints require `Authorization: Bearer <jwt>` (validated by `_check_jwt_auth()`).
- WebSocket handshake uses `?token=<nbwt_*>` query param (browser WebSocket API can't set headers).

**User isolation** (session key format):

- Session keys follow the pattern `channel:user_id:chat_id` (e.g. `websocket:abc123:def456`).
- `_session_key_for_user()` in `websocket.py` builds user-scoped keys.
- Session listing filters by user prefix; media directories are namespaced per user.
- Without auth (legacy localhost mode), keys fall back to `channel:chat_id`.

**Rule**: Any new endpoint that accesses sessions or user data must go through `_require_auth()` or `_require_admin()`. Never mix user data across sessions.
