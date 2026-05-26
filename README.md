# nanobot — Enterprise AI Agent Platform

> Built on the lightweight agent core of [nanobot](https://github.com/HKUDS/nanobot) by [Xubin Ren](https://github.com/re-bin), extended with enterprise-grade multi-tenancy, authentication, and group-based access control.

## Key Features

- **Multi-user authentication** — JWT + bcrypt login, SQLite-backed user store, first-run admin setup
- **Role-based access control** — Admin and user roles, admin-only settings and user management
- **Group workspaces** — Create groups, manage memberships, share skills within groups, control access per group
- **Per-user isolation** — Independent sessions, workspaces, memory, cron jobs, and history for every user
- **WebUI with auth** — Login/setup flow, JWT persistence, 401 redirect, admin/read-only UI separation
- **OpenAI-compatible API** — Programmatic access via `/v1/chat/completions` and `/v1/models`
- **Multi-provider LLM** — Anthropic, OpenAI, Azure, Bedrock, GitHub Copilot, OpenAI Codex, and more
- **Tool ecosystem** — Filesystem, shell sandbox, web search/fetch, MCP servers, cron, image generation, subagents
- **Memory & persistence** — Dream two-phase memory consolidation, atomic writes, session TTL compaction

## Quick Start

```bash
git clone https://github.com/FFshine/nanobot.git
cd nanobot
pip install -e .

# Interactive setup — configure your LLM provider
nanobot onboard

# Start the gateway
nanobot gateway
```

Open `http://127.0.0.1:8765` — the first user to sign up becomes admin.

## Configuration

```json
{
  "channels": { "websocket": { "enabled": true } },
  "providers": {
    "openrouter": { "apiKey": "sk-or-v1-xxx" }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-4-6"
    }
  }
}
```

Full configuration options at [nanobot.wiki](https://nanobot.wiki/docs/latest/getting-started/nanobot-overview).

## Architecture

```
Browser (WebUI) ──WebSocket──▶ Gateway (:8765)
                                   │
                     ┌─────────────┼─────────────┐
                     ▼             ▼             ▼
               Auth (JWT)    Agent Loop    Session Mgr
               User DB       LLM Provider  Per-user isolation
               Group DB      Tools/MCP     Memory/History
```

## User & Group Management

All managed through the WebUI:

- **Settings → Users** — Create, list, and delete users (admin only)
- **Settings → Groups** — Create groups, manage members and roles, share skills, control access

API endpoints:

| Endpoint | Access |
|----------|--------|
| `GET /api/auth/me` | Current user info |
| `GET /api/me/groups` | Current user's group memberships |
| `GET /api/groups` | List all groups |
| `GET /api/auth/users` | List all users (admin) |

## License

MIT
