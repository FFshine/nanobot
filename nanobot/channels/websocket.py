"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import hashlib
import hmac
import http
import json
import mimetypes
import re
import secrets
import shutil
import ssl
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.agent.tools.mcp import request_mcp_reload
from nanobot.auth import (
    authenticate_user,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_count,
    list_users,
)
from nanobot.auth.tokens import create_token, verify_token
from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.command.builtin import builtin_command_palette
from nanobot.config.paths import get_media_dir, get_workspace_path
from nanobot.config.schema import Base
from nanobot.session.goal_state import goal_state_ws_blob
from nanobot.session.webui_turns import websocket_turn_wall_started_at
from nanobot.utils.helpers import safe_filename, sync_workspace_templates
from nanobot.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanobot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanobot.webui.cli_apps_api import (
    cli_apps_action,
    cli_apps_payload,
    normalize_cli_app_mentions,
)
from nanobot.webui.mcp_presets_api import (
    mcp_presets_settings_action,
    normalize_mcp_preset_mentions,
)
from nanobot.webui.settings_api import (
    WebUISettingsError,
    create_model_configuration,
    group_create,
    group_delete,
    group_detail_payload,
    group_member_add,
    group_member_remove,
    group_members_list,
    group_skill_content,
    group_skill_create,
    group_skill_delete,
    group_skill_update,
    group_skills_list,
    group_update,
    groups_list_payload,
    settings_payload,
    update_agent_settings,
    update_image_generation_settings,
    update_provider_settings,
    update_web_search_settings,
)
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import (
    append_transcript_object,
    build_webui_thread_response,
    rewrite_local_markdown_images,
)

_MCP_PRESET_ACTIONS_BY_PATH = {
    "/api/settings/mcp-presets/enable": "enable",
    "/api/settings/mcp-presets/remove": "remove",
    "/api/settings/mcp-presets/test": "test",
    "/api/settings/mcp-presets/custom": "custom",
    "/api/settings/mcp-presets/import": "import",
    "/api/settings/mcp-presets/import-cursor": "import-cursor",
    "/api/settings/mcp-presets/tools": "tools",
}
_MCP_VALUES_HEADER = "X-Nanobot-MCP-Values"
_MCP_VALUES_HEADER_MAX_BYTES = 64 * 1024

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      nanobot and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Nanobot-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _default_model_name_from_config() -> str | None:
    """Resolved model string from on-disk config (bootstrap fallback)."""
    try:
        from nanobot.config.loader import load_config

        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _parse_mcp_settings_query(request: WsRequest) -> dict[str, list[str]]:
    query = _parse_query(request.path)
    raw = request.headers.get(_MCP_VALUES_HEADER)
    if not raw:
        return query
    if len(raw.encode("utf-8")) > _MCP_VALUES_HEADER_MAX_BYTES:
        raise WebUISettingsError("MCP settings payload is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebUISettingsError("invalid MCP settings payload") from exc
    if not isinstance(payload, dict):
        raise WebUISettingsError("MCP settings payload must be a JSON object")
    merged = {key: list(values) for key, values in query.items()}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise WebUISettingsError("MCP settings payload contains an invalid key")
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if text:
            merged[key] = [text]
    return merged


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    msg = message or http.HTTPStatus(status).phrase
    body = json.dumps({"error": msg, "status": status}, ensure_ascii=False).encode("utf-8")
    return _http_response(body, status=status, content_type="application/json; charset=utf-8")


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _parse_basic_auth(headers: Any) -> tuple[str, str]:
    """Extract username and password from Basic Auth header; returns ('', '') on failure."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
        if ":" not in decoded:
            return "", ""
        username, password = decoded.split(":", 1)
        return username, password
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return "", ""


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding — compact + friendly in URL paths."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Reverse of :func:`_b64url_encode`; caller handles ``ValueError``."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})
def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Nanobot-Auth") or headers.get("x-nanobot-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        workspace_path: Path | None = None,
        runtime_model_name: Callable[[], str | None] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for HTTP routes served beside WS; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._workspace_path = (
            Path(workspace_path).expanduser()
            if workspace_path is not None
            else get_workspace_path()
        ).resolve(strict=False)
        self._runtime_model_name = runtime_model_name
        self._settings_restart_sections: set[str] = set()
        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)
        # WS handshake token -> user_id mapping for user-aware connections
        self._ws_token_user: dict[str, str] = {}
        # connection -> user_id for the lifetime of a WS connection
        self._conn_user: dict[Any, str] = {}
        # chat_id -> user_id for session-scoping without connection
        self._chat_user: dict[str, str] = {}
        # Per-user component factories — lazily created on first access
        self._user_workspaces: dict[str, Path] = {}
        self._user_session_managers: dict[str, "SessionManager"] = {}
        self._user_cron_services: dict[str, "CronService"] = {}

    # -- Per-user component helpers -------------------------------------------

    def _get_user_workspace(self, user_id: str) -> Path:
        if user_id not in self._user_workspaces:
            ws = get_workspace_path(user_id=user_id)
            sync_workspace_templates(ws, silent=True)
            self._user_workspaces[user_id] = ws
        return self._user_workspaces[user_id]

    def _get_user_session_manager(self, user_id: str) -> "SessionManager":
        if user_id not in self._user_session_managers:
            # Legacy tokens resolve to __legacy__; use the global store when
            # one was injected (production or test) so pre-migration data and
            # test mocks are reachable.
            if self._session_manager is not None and user_id == "__legacy__":
                return self._session_manager
            from nanobot.session.manager import SessionManager

            ws = self._get_user_workspace(user_id)
            self._user_session_managers[user_id] = SessionManager(ws)
        return self._user_session_managers[user_id]

    def _get_user_cron_service(self, user_id: str) -> "CronService":
        from nanobot.cron.service import CronService
        from nanobot.cron.types import CronJob, CronPayload
        from nanobot.config.loader import load_config

        if user_id not in self._user_cron_services:
            ws = self._get_user_workspace(user_id)
            svc = CronService(ws / "cron" / "jobs.json")

            # Seed the dream system job for WebUI visibility
            config = load_config()
            dream_cfg = config.agents.defaults.dream
            svc.register_system_job(CronJob(
                id="dream",
                name="dream",
                schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
                payload=CronPayload(kind="system_event"),
            ))

            self._user_cron_services[user_id] = svc
        return self._user_cron_services[user_id]

    def _get_user_webui_dir(self, user_id: str) -> Path:
        from nanobot.config.paths import get_webui_dir

        return get_webui_dir(user_id=user_id)

    # -- Auth helpers ---------------------------------------------------------

    def _check_jwt_auth(self, request: WsRequest) -> str | None:
        """Validate JWT or API token; return user_id or None."""
        # Try JWT (Bearer) first
        token = _bearer_token(request.headers)
        if token and not token.startswith("nbwt_"):
            payload = verify_token(token)
            if payload is not None:
                return payload.user_id
        # Fall back to legacy nbwt_* token pool
        if self._check_api_token(request):
            return "__legacy__"
        return None

    def _get_user_from_request(self, request: WsRequest) -> dict | None:
        """Extract and validate user identity from request; return user dict or None.

        Returns a synthetic ``__legacy__`` user for legacy API tokens so that
        session-ownership checks can skip user-scoped filtering.
        """
        user_id = self._check_jwt_auth(request)
        if user_id is None:
            return None
        if user_id == "__legacy__":
            return {"id": "__legacy__", "username": "legacy", "displayName": "Legacy", "role": "admin"}
        user = get_user_by_id(user_id)
        return user.to_public() if user else None

    def _require_auth(self, request: WsRequest) -> dict | None:
        """Require valid auth; returns user dict or sends HTTP error response."""
        return self._get_user_from_request(request)

    def _require_admin(self, request: WsRequest) -> dict | None:
        """Require admin auth."""
        user = self._require_auth(request)
        if user is None:
            return None
        if user.get("role") != "admin":
            return None
        return user

    def _require_admin_for_token(self, request: WsRequest) -> dict | None:
        """Validate JWT or WS token and check admin role. Returns user dict or None."""
        # First try JWT (Bearer) via _get_user_from_request
        user = self._get_user_from_request(request)
        if user is not None:
            if user.get("role") != "admin":
                return None
            return user
        # Fall back to query string WS token
        token = _query_first(_parse_query(request.path), "token") or ""
        user_id = self._ws_token_user.get(token, "")
        if not user_id:
            return None
        user_obj = get_user_by_id(user_id)
        if user_obj is None or user_obj.role != "admin":
            return None
        return user_obj.to_public()

    def _require_group_member_or_admin(
        self, request: WsRequest, group_id: str
    ) -> dict | None:
        """Require admin role or membership in *group_id*. Returns user dict or None."""
        user = self._get_user_from_request(request)
        if user is None:
            return None
        if user.get("role") == "admin":
            return user
        from nanobot.auth import get_user_groups

        if any(g.id == group_id for g in get_user_groups(user["id"])):
            return user
        return None

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)
        user_id = self._conn_user.get(connection, "")
        if user_id:
            self._chat_user[chat_id] = user_id

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)
        self._conn_user.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        user_id = self._chat_user.get(chat_id, "")
        sm = self._get_user_session_manager(user_id) if user_id else None
        if sm is None:
            sm = self._session_manager
        if sm is None:
            return
        sk = self._session_key_for_user(chat_id, user_id)
        row = sm.read_session_file(sk)
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    _MAX_ISSUED_TOKENS = 10_000

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt).

        Uses single-step pop to minimize the window between lookup and removal;
        safe under asyncio's single-threaded cooperative model.
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response(
            {"token": token_value, "expires_in": self.config.token_ttl_s}
        )

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path."""
        got, query = _parse_request_path(request.path)

        # 1. Token issue endpoint (legacy, optional, gated by configured secret).
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 2. Bootstrap (`/webui/bootstrap`): mint WS/API tokens + shared session metadata.
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. Auth endpoints
        if got == "/api/auth/me":
            return self._handle_auth_me(request)
        if got == "/api/me/groups":
            return self._handle_me_groups(request, connection)
        if got == "/api/auth/setup":
            return self._handle_auth_setup(request)
        if got == "/api/auth/users":
            return self._handle_auth_users_list(request)
        m = re.match(r"^/api/auth/users/([^/]+)/delete$", got)
        if m:
            return self._handle_auth_user_delete(request, m.group(1))
        if got == "/api/auth/users/create":
            return self._handle_auth_user_create(request)

        # 4. REST handlers co-located with this channel (sessions, settings, …).
        if got == "/api/sessions":
            return self._handle_sessions_list(request)

        if got == "/api/settings":
            return self._handle_settings(request)

        if got == "/api/settings/profile":
            return self._handle_settings_profile(request)

        if got == "/api/settings/skills":
            return self._handle_settings_skills(request)

        if got == "/api/settings/cron":
            return self._handle_settings_cron(request)

        m = re.match(r"^/api/settings/cron/([^/]+)/delete$", got)
        if m:
            return self._handle_settings_cron_delete(request, m.group(1))

        m = re.match(r"^/api/settings/skills/([^/]+)/delete$", got)
        if m:
            return self._handle_settings_skill_delete(request, m.group(1))

        m = re.match(r"^/api/settings/skills/([^/]+)/content$", got)
        if m:
            return self._handle_settings_skill_content(request, m.group(1))

        m = re.match(r"^/api/settings/skills/([^/]+)/update$", got)
        if m:
            return self._handle_settings_skill_update(request, m.group(1))

        m = re.match(r"^/api/settings/skills/create$", got)
        if m:
            return self._handle_settings_skill_create(request)

        if got == "/api/commands":
            return self._handle_commands(request)

        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)

        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)

        if got == "/api/settings/update":
            return self._handle_settings_update(request)

        if got == "/api/settings/model-configurations/create":
            return self._handle_settings_model_configuration_create(request)

        if got == "/api/settings/provider/update":
            return self._handle_settings_provider_update(request)

        if got == "/api/settings/web-search/update":
            return self._handle_settings_web_search_update(request)

        if got == "/api/settings/image-generation/update":
            return self._handle_settings_image_generation_update(request)

        if got == "/api/settings/cli-apps":
            return self._handle_settings_cli_apps(request)

        if got == "/api/settings/cli-apps/install":
            return await self._handle_settings_cli_apps_action(request, "install")

        if got == "/api/settings/cli-apps/update":
            return await self._handle_settings_cli_apps_action(request, "update")

        if got == "/api/settings/cli-apps/uninstall":
            return await self._handle_settings_cli_apps_action(request, "uninstall")

        if got == "/api/settings/cli-apps/test":
            return await self._handle_settings_cli_apps_action(request, "test")

        if got == "/api/settings/mcp-presets":
            return await self._handle_settings_mcp_presets(request)

        mcp_action = _MCP_PRESET_ACTIONS_BY_PATH.get(got)
        if mcp_action is not None:
            return await self._handle_settings_mcp_presets(request, mcp_action)

        # -- Group API routes --
        if got == "/api/groups":
            return self._handle_groups_list(request)

        if got == "/api/groups/create":
            return self._handle_group_create(request)

        m = re.match(r"^/api/groups/([^/]+)$", got)
        if m:
            return self._handle_group_detail(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/update$", got)
        if m:
            return self._handle_group_update(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/delete$", got)
        if m:
            return self._handle_group_delete(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/members$", got)
        if m:
            return self._handle_group_members(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/members/add$", got)
        if m:
            return self._handle_group_member_add(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/members/([^/]+)/remove$", got)
        if m:
            return self._handle_group_member_remove(request, m.group(1), m.group(2))

        m = re.match(r"^/api/groups/([^/]+)/skills$", got)
        if m:
            return self._handle_group_skills(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/skills/create$", got)
        if m:
            return self._handle_group_skill_create(request, m.group(1))

        m = re.match(r"^/api/groups/([^/]+)/skills/([^/]+)/content$", got)
        if m:
            return self._handle_group_skill_content(request, m.group(1), m.group(2))

        m = re.match(r"^/api/groups/([^/]+)/skills/([^/]+)/update$", got)
        if m:
            return self._handle_group_skill_update(request, m.group(1), m.group(2))

        m = re.match(r"^/api/groups/([^/]+)/skills/([^/]+)/delete$", got)
        if m:
            return self._handle_group_skill_delete(request, m.group(1), m.group(2))

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        # NOTE: websockets' HTTP parser only accepts GET, so we cannot expose a
        # true ``DELETE`` verb. The action is folded into the path instead.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        # Signed media fetch: ``<sig>`` is an HMAC over ``<payload>``; the
        # payload decodes to a path inside :func:`get_media_dir`. See
        # :meth:`_sign_media_path` for the inverse direction used to build
        # these URLs when replaying a session.
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2))

        # 4. WebSocket upgrade (the channel's primary purpose). Only run the
        # handshake gate on requests that actually ask to upgrade; otherwise
        # a bare ``GET /`` from the browser would be rejected as an
        # unauthorized WS handshake instead of serving the SPA's index.html.
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            # Associate WS token with user for session-scoping.
            # Keep the token in _ws_token_user (using get, not pop) so
            # settings write endpoints can verify admin role via the token.
            ws_token = _query_first(query, "token") or ""
            user_id = self._ws_token_user.get(ws_token, "")
            self._conn_user[connection] = user_id
            return self._authorize_websocket_handshake(connection, query)

        # 5. Static SPA serving (only if a build directory was wired in).
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    # -- HTTP route handlers ------------------------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        token = _bearer_token(request.headers) or _query_first(
            _parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        # Support Basic Auth (username:password) for user login,
        # JWT Bearer token (for session refresh), and legacy shared-secret mode.
        user = None
        username, password = _parse_basic_auth(request.headers)
        if username and password:
            user = authenticate_user(username, password)
            if user is None:
                return _http_error(401, "Invalid credentials")
        else:
            # Try JWT Bearer token for session refresh (no password needed)
            user_id = self._check_jwt_auth(request)
            if user_id and user_id != "__legacy__":
                user = get_user_by_id(user_id)
            if user is None:
                # Legacy: shared secret or localhost-only
                secret = self.config.token_issue_secret.strip() or self.config.token.strip()
                if secret:
                    if not _issue_route_secret_matches(request.headers, secret):
                        return _http_error(401, "Unauthorized")
                elif not _is_localhost(connection):
                    return _http_error(403, "bootstrap is localhost-only")

        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_json_response(
                {"error": "too many outstanding tokens"}, status=429
            )

        ws_token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        self._issued_tokens[ws_token] = expiry
        self._api_tokens[ws_token] = expiry

        resp: dict[str, Any] = {
            "ws_token": ws_token,
            "ws_path": self._expected_path(),
            "expires_in": self.config.token_ttl_s,
            "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
            "has_users": get_user_count() > 0,
        }
        if user is not None:
            jwt_token = create_token(user.id, user.username, user.role)
            resp["token"] = jwt_token
            resp["user"] = user.to_public()
            self._ws_token_user[ws_token] = user.id
        return _http_json_response(resp)

    # -- Auth endpoint handlers ------------------------------------------------

    def _handle_auth_me(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        return _http_json_response({"user": user})

    def _handle_me_groups(self, request: WsRequest, connection: Any) -> Response:
        """Return the authenticated user's group memberships with roles."""
        user = self._require_auth(request)
        if user is not None:
            user_id = user["id"]
        elif _is_localhost(connection):
            query = _parse_query(request.path)
            user_id = (_query_first(query, "user_id") or "").strip()
            if not user_id:
                return _http_error(400, "user_id required")
        else:
            return _http_error(401, "Unauthorized")
        try:
            from nanobot.auth import get_user_groups, get_group_members

            groups = get_user_groups(user_id)
            result = []
            for g in groups:
                role = "member"
                for m in get_group_members(g.id):
                    if m.user_id == user_id:
                        role = m.role
                        break
                result.append({
                    "id": g.id,
                    "name": g.name,
                    "displayName": g.display_name,
                    "role": role,
                })
            return _http_json_response({"groups": result})
        except Exception as e:
            logger.warning("Failed to fetch user groups: {}", e)
            return _http_error(500, str(e))

    def _handle_auth_setup(self, request: WsRequest) -> Response:
        """Create the first admin user when no users exist."""
        if get_user_count() > 0:
            return _http_error(403, "Setup only available when no users exist")
        query = _parse_query(request.path)
        username = (_query_first(query, "username") or "").strip()
        password = (_query_first(query, "password") or "").strip()
        if not username or not password:
            return _http_error(400, "username and password are required")
        if len(password) < 6:
            return _http_error(400, "Password must be at least 6 characters")
        try:
            user = create_user(username, password, display_name=username, role="admin")
            return _http_json_response({"ok": True, "user": user.to_public()})
        except Exception:
            return _http_error(409, "Username already exists")

    def _handle_auth_users_list(self, request: WsRequest) -> Response:
        admin = self._require_admin(request)
        if admin is None:
            return _http_error(403, "Admin only")
        users = [u.to_public() for u in list_users()]
        return _http_json_response({"users": users})

    def _handle_auth_user_delete(self, request: WsRequest, target_id: str) -> Response:
        admin = self._require_admin(request)
        if admin is None:
            return _http_error(403, "Admin only")
        if admin["id"] == target_id:
            return _http_error(400, "Cannot delete yourself")
        ok = delete_user(target_id)
        if not ok:
            return _http_error(404, "User not found")
        return _http_json_response({"ok": True})

    def _handle_auth_user_create(self, request: WsRequest) -> Response:
        admin = self._require_admin(request)
        if admin is None:
            return _http_error(403, "Admin only")
        query = _parse_query(request.path)
        username = (_query_first(query, "username") or "").strip()
        password = (_query_first(query, "password") or "").strip()
        role = (_query_first(query, "role") or "user").strip()
        if not username or not password:
            return _http_error(400, "username and password are required")
        if len(password) < 6:
            return _http_error(400, "Password must be at least 6 characters")
        if role not in ("admin", "user"):
            return _http_error(400, "role must be 'admin' or 'user'")
        try:
            user = create_user(username, password, display_name=username, role=role)
            return _http_json_response({"ok": True, "user": user.to_public()})
        except Exception:
            return _http_error(409, "Username already exists")

    def _session_key_for_user(self, chat_id: str, user_id: str = "") -> str:
        """Build a user-scoped session key."""
        if user_id:
            return f"websocket:{user_id}:{chat_id}"
        return f"websocket:{chat_id}"

    def _user_from_connection(self, connection: Any) -> str:
        return self._conn_user.get(connection, "")

    # -- Session / settings handlers ------------------------------------------

    @staticmethod
    def _session_owned_by_user(key: str, user_id: str) -> bool:
        """Check that *key* belongs to *user_id*.

        Legacy tokens resolve to ``__legacy__`` which cannot be mapped to a
        specific user, so the check is skipped in that case.
        """
        if user_id == "__legacy__":
            return True
        return key.startswith(f"websocket:{user_id}:")

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        user_id = user["id"]
        sm = self._get_user_session_manager(user_id)
        sessions = sm.list_sessions()
        cleaned = []
        if user_id == "__legacy__":
            key_prefix = "websocket:"
        else:
            key_prefix = f"websocket:{user_id}:"
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith(key_prefix)):
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            chat_id = key.split(":", 2)[-1] if ":" in key else key
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            cleaned.append(row)
        return _http_json_response({"sessions": cleaned})

    def _handle_settings(self, request: WsRequest) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._with_settings_restart_state(settings_payload()))

    def _handle_settings_profile(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import profile_files_payload

        ws = self._get_user_workspace(user["id"])
        return _http_json_response(profile_files_payload(str(ws)))

    def _handle_settings_skills(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import skills_list_payload

        ws = self._get_user_workspace(user["id"])
        return _http_json_response(skills_list_payload(str(ws), user_id=user["id"]))

    def _handle_settings_cron(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import cron_list_payload

        ws = self._get_user_workspace(user["id"])
        return _http_json_response(cron_list_payload(user["id"], str(ws)))

    def _handle_settings_cron_delete(self, request: WsRequest, job_id: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import cron_delete_job

        svc = self._get_user_cron_service(user["id"])
        ok = cron_delete_job(svc, job_id)
        if not ok:
            return _http_error(404, "Job not found")
        return _http_json_response({"deleted": job_id})

    def _handle_settings_skill_delete(self, request: WsRequest, name: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import delete_user_skill

        ws = self._get_user_workspace(user["id"])
        try:
            deleted = delete_user_skill(str(ws), name)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response({"deleted": deleted})

    def _handle_settings_skill_content(self, request: WsRequest, name: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from pathlib import Path

        from nanobot.agent.skills import SkillsLoader
        from nanobot.agent.tools.context import bind_group_workspaces

        # Bind group workspaces so SkillsLoader can find group skills.
        try:
            from nanobot.auth import get_user_groups
            from nanobot.config.paths import get_group_workspace_path

            group_ws = [get_group_workspace_path(g.id) for g in get_user_groups(user["id"])]
            bind_group_workspaces(group_ws)
        except Exception:
            pass

        ws = self._get_user_workspace(user["id"])
        loader = SkillsLoader(ws)
        content = loader.load_skill(name) or ""
        return _http_json_response({"name": name, "content": content})

    def _handle_settings_skill_update(self, request: WsRequest, name: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import update_user_skill_content

        query = _parse_query(request.path)
        content = _query_first(query, "content")
        if content is None:
            return _http_error(400, "missing content")
        ws = self._get_user_workspace(user["id"])
        try:
            update_user_skill_content(str(ws), name, content)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response({"ok": True})

    def _handle_settings_skill_create(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        from nanobot.webui.settings_api import create_user_skill

        query = _parse_query(request.path)
        name = _query_first_alias(query, "name") or ""
        content = _query_first(query, "content")
        if not name or content is None:
            return _http_error(400, "missing name or content")
        ws = self._get_user_workspace(user["id"])
        try:
            result = create_user_skill(str(ws), name, content)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(result)

    # -- Group API handlers --------------------------------------------------

    def _handle_groups_list(self, request: WsRequest) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(groups_list_payload())

    def _handle_group_create(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        name = (_query_first(query, "name") or "").strip()
        if not name:
            return _http_error(400, "name is required")
        display_name = _query_first(query, "display_name") or ""
        settings_str = _query_first(query, "settings") or "{}"
        try:
            import json
            settings = json.loads(settings_str)
        except (json.JSONDecodeError, TypeError):
            return _http_error(400, "invalid settings JSON")
        try:
            result = group_create(name=name, display_name=display_name, settings=settings)
        except Exception:
            return _http_error(409, "Group name already exists")
        return _http_json_response(result)

    def _handle_group_detail(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        detail = group_detail_payload(group_id)
        if detail is None:
            return _http_error(404, "Group not found")
        return _http_json_response(detail)

    def _handle_group_update(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        display_name = _query_first(query, "display_name")
        settings_str = _query_first(query, "settings")
        settings = None
        if settings_str is not None:
            try:
                import json
                settings = json.loads(settings_str)
            except (json.JSONDecodeError, TypeError):
                return _http_error(400, "invalid settings JSON")
        result = group_update(group_id, display_name=display_name, settings=settings)
        if result is None:
            return _http_error(404, "Group not found")
        return _http_json_response(result)

    def _handle_group_delete(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        ok = group_delete(group_id)
        if not ok:
            return _http_error(404, "Group not found")
        return _http_json_response({"deleted": group_id})

    def _handle_group_members(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        members = group_members_list(group_id)
        if members is None:
            return _http_error(404, "Group not found")
        return _http_json_response(members)

    def _handle_group_member_add(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        user_id = (_query_first(query, "user_id") or "").strip()
        role = (_query_first(query, "role") or "member").strip()
        if not user_id:
            return _http_error(400, "user_id is required")
        try:
            result = group_member_add(group_id, user_id, role=role)
        except Exception:
            return _http_error(400, "Failed to add member")
        return _http_json_response(result)

    def _handle_group_member_remove(self, request: WsRequest, group_id: str, user_id: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        ok = group_member_remove(group_id, user_id)
        if not ok:
            return _http_error(404, "Member not found")
        return _http_json_response({"removed": user_id})

    def _handle_group_skills(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_group_member_or_admin(request, group_id):
            return _http_error(403, "Forbidden")
        skills = group_skills_list(group_id)
        if skills is None:
            return _http_error(404, "Group not found")
        return _http_json_response(skills)

    def _handle_group_skill_create(self, request: WsRequest, group_id: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        name = (_query_first(query, "name") or "").strip()
        content = _query_first(query, "content") or ""
        if not name:
            return _http_error(400, "name is required")
        import re as _re
        if not _re.match(r"^[a-z0-9_-]+$", name):
            return _http_error(400, "invalid skill name")
        result = group_skill_create(group_id, name, content)
        return _http_json_response(result)

    def _handle_group_skill_content(self, request: WsRequest, group_id: str, name: str) -> Response:
        if not self._require_group_member_or_admin(request, group_id):
            return _http_error(403, "Forbidden")
        content = group_skill_content(group_id, name)
        if content is None:
            return _http_error(404, "Skill not found")
        return _http_json_response(content)

    def _handle_group_skill_update(self, request: WsRequest, group_id: str, name: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        content = _query_first(query, "content")
        if content is None:
            return _http_error(400, "missing content")
        result = group_skill_update(group_id, name, content)
        return _http_json_response({"ok": True, "name": result["name"]})

    def _handle_group_skill_delete(self, request: WsRequest, group_id: str, name: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        ok = group_skill_delete(group_id, name)
        if not ok:
            return _http_error(404, "Skill not found")
        return _http_json_response({"deleted": name})

    def _with_settings_restart_state(
        self,
        payload: dict[str, Any],
        *,
        section: str | None = None,
    ) -> dict[str, Any]:
        """Keep restart-required state alive for this gateway process."""
        if section and payload.get("requires_restart"):
            self._settings_restart_sections.add(section)
        if self._settings_restart_sections:
            payload = dict(payload)
            payload["requires_restart"] = True
            payload["restart_required_sections"] = sorted(self._settings_restart_sections)
        else:
            payload = dict(payload)
            payload["restart_required_sections"] = []
        return payload

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state(user_id=user["id"]))

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded, user_id=user["id"])
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self.logger.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    def _handle_settings_update(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = update_agent_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(
            self._with_settings_restart_state(payload, section="runtime")
        )

    def _handle_settings_model_configuration_create(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = create_model_configuration(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload))

    def _handle_settings_provider_update(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = update_provider_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="image"))

    def _handle_settings_web_search_update(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = update_web_search_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="web"))

    def _handle_settings_image_generation_update(self, request: WsRequest) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = update_image_generation_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="image"))

    def _handle_settings_cli_apps(self, request: WsRequest) -> Response:
        if not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        try:
            payload = cli_apps_payload()
        except Exception:
            self.logger.exception("failed to load CLI Apps payload")
            return _http_error(500, "failed to load CLI Apps")
        return _http_json_response(payload)

    async def _handle_settings_cli_apps_action(self, request: WsRequest, action: str) -> Response:
        if not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        query = _parse_query(request.path)
        try:
            payload = await asyncio.to_thread(cli_apps_action, action, query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        except Exception as e:
            status = getattr(e, "status", 500)
            message = getattr(e, "message", str(e))
            if status >= 500:
                self.logger.exception("CLI Apps action '{}' failed", action)
            return _http_error(status, message)
        return _http_json_response(payload)

    async def _handle_settings_mcp_presets(
        self,
        request: WsRequest,
        action: str | None = None,
    ) -> Response:
        if action is not None and not self._require_admin_for_token(request):
            return _http_error(403, "Admin required")
        if action is None and not self._require_auth(request):
            return _http_error(401, "Unauthorized")
        try:
            payload = await mcp_presets_settings_action(
                action,
                _parse_mcp_settings_query(request),
                reload_mcp=lambda: request_mcp_reload(self.bus),
            )
        except Exception as e:
            status = getattr(e, "status", 500)
            message = getattr(e, "message", str(e))
            if status >= 500:
                self.logger.exception("MCP preset action '{}' failed", action or "list")
            return _http_error(status, message)
        if action is None:
            return _http_json_response(payload)
        return _http_json_response(
            self._with_settings_restart_state(payload, section="runtime")
        )

    @staticmethod
    def _is_websocket_channel_session_key(key: str) -> bool:
        """True when *key* is a ``websocket:…`` session exposed on this HTTP surface."""
        return key.startswith("websocket:")

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._session_owned_by_user(decoded_key, user["id"]):
            return _http_error(403, "Forbidden")
        # Only ``websocket:…`` sessions are listed/served here — same boundary as
        # ``/api/sessions``. Block handcrafted URLs from probing CLI / Slack / etc.
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        sm = self._get_user_session_manager(user["id"])
        data = sm.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        # Decorate persisted user messages with signed media URLs so the
        # client can render previews. The raw on-disk ``media`` paths are
        # stripped on the way out — they leak server filesystem layout and
        # the client never needs them once it has the signed fetch URL.
        self._augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._session_owned_by_user(decoded_key, user["id"]):
            return _http_error(403, "Forbidden")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self._augment_transcript_user_media,
            augment_assistant_text=self._rewrite_local_markdown_images,
            user_id=user["id"],
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        return _http_json_response(data)

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        user_id = self._chat_user.get(chat_id, "")
        sk = self._session_key_for_user(chat_id, user_id)
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            append_transcript_object(sk, dup, user_id=user_id)
        except (ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)

    def _augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pstr in paths:
            path = Path(pstr)
            att = self._sign_or_stage_media_path(path)
            if att is None:
                continue
            mime, _ = mimetypes.guess_type(path.name)
            kind = "video" if mime and mime.startswith("video/") else "image"
            out.append(
                {"kind": kind, "url": att["url"], "name": att.get("name", path.name)},
            )
        return out

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        user_id = self._chat_user.get(chat_id, "")
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            cli_apps = meta.get("cli_apps")
            if isinstance(cli_apps, list) and cli_apps:
                user_obj["cli_apps"] = cli_apps
            mcp_presets = meta.get("mcp_presets")
            if isinstance(mcp_presets, list) and mcp_presets:
                user_obj["mcp_presets"] = mcp_presets
            self._try_append_webui_transcript(chat_id, user_obj)
        if not session_key and user_id:
            session_key = self._session_key_for_user(chat_id, user_id)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
            is_dm,
        )

    def _augment_media_urls(self, payload: dict[str, Any]) -> None:
        """Mutate *payload* in place: each message's ``media`` path list is
        replaced by a parallel ``media_urls`` list of signed fetch URLs.

        Messages without media or with non-string path entries are left
        untouched. Paths that no longer live inside ``media_dir`` (e.g. the
        file was deleted, or the dir was relocated) are silently skipped;
        the client falls back to the historical-replay placeholder tile.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            media = msg.get("media")
            if not isinstance(media, list) or not media:
                continue
            urls: list[dict[str, str]] = []
            for entry in media:
                if not isinstance(entry, str) or not entry:
                    continue
                signed = self._sign_media_path(Path(entry))
                if signed is None:
                    continue
                urls.append({"url": signed, "name": Path(entry).name})
            if urls:
                msg["media_urls"] = urls
            # Always drop the raw paths from the wire payload.
            msg.pop("media", None)

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or
        ``None`` when the path does not resolve inside the media root.

        The URL is self-authenticating: the signature binds the payload to
        this process's ``_media_secret``, so only paths we chose to sign can
        be fetched. The returned path is relative to the server origin; the
        client joins it against this server's HTTP origin (same host as WS).
        """
        try:
            media_root = get_media_dir().resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*.

        Persisted inbound media already lives under ``get_media_dir`` and can
        be signed directly. Outbound bot-generated files may live anywhere on
        disk; copy those into the websocket media bucket first so the browser
        can fetch them through the existing signed media route without
        exposing arbitrary filesystem paths.
        """
        signed = self._sign_media_path(path)
        if signed is not None:
            return {"url": signed, "name": path.name}
        try:
            if not path.is_file():
                return None
            media_dir = get_media_dir("websocket")
            safe_name = safe_filename(path.name) or "attachment"
            staged = media_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            shutil.copyfile(path, staged)
        except OSError as exc:
            self.logger.warning("failed to stage outbound media {}: {}", path, exc)
            return None
        signed = self._sign_media_path(staged)
        if signed is None:
            return None
        return {"url": signed, "name": path.name}

    def _rewrite_local_markdown_images(self, text: str) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=self._workspace_path,
            sign_path=self._sign_or_stage_media_path,
        )

    def _handle_media_fetch(self, sig: str, payload: str) -> Response:
        """Serve a single media file previously signed via
        :meth:`_sign_media_path`. Validates the signature, decodes the
        payload to a relative path, and streams the file bytes with a
        long-lived immutable cache header (the URL already encodes the
        file identity, so caches can be aggressive)."""
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return _http_error(401, "invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return _http_error(401, "invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return _http_error(400, "invalid payload")
        # An attacker who somehow bypassed the HMAC check would still need
        # the resolved path to escape the media root; guard defensively.
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return _http_error(404, "not found")
        if not candidate.is_file():
            return _http_error(404, "not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return _http_error(500, "read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return _http_response(
            body,
            content_type=mime,
            extra_headers=[
                ("Cache-Control", "private, max-age=31536000, immutable"),
                # Paired with the MIME whitelist above: prevents browsers from
                # MIME-sniffing an octet-stream fallback into executable HTML.
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        user = self._require_auth(request)
        if user is None:
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._session_owned_by_user(decoded_key, user["id"]):
            return _http_error(403, "Forbidden")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        sm = self._get_user_session_manager(user["id"])
        deleted = sm.delete_session(decoded_key)
        delete_webui_thread(decoded_key, user_id=user["id"])
        return _http_json_response({"deleted": bool(deleted)})

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        # Reject path-traversal attempts and absolute targets.
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history-mode fallback: unknown routes serve index.html so the
            # client-side router can render them.
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self.logger.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # Hash-named build assets are cache-friendly; index.html must stay fresh.
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    async def start(self) -> None:
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}://{}:{}{}",
            scheme,
            self.config.host,
            self.config.port,
            self.config.path,
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}://{}:{}{}",
                scheme,
                self.config.host,
                self.config.port,
                _normalize_config_path(self.config.token_issue_path),
            )

        async def runner() -> None:
            async with serve(
                handler,
                self.config.host,
                self.config.port,
                process_request=process_request,
                max_size=self.config.max_message_bytes,
                ping_interval=self.config.ping_interval_s,
                ping_timeout=self.config.ping_timeout_s,
                ssl=ssl_context,
            ):
                assert self._stop_event is not None
                await self._stop_event.wait()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket already authenticates at handshake time (token),
                # so pairing is not applicable. Treat as non-DM to avoid
                # sending pairing codes to an already-authenticated client.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    def _save_envelope_media(
        self,
        media: list[Any],
        user_id: str = "",
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket", user_id=user_id) if user_id else get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(
                    raw_media, user_id=self._conn_user.get(connection, "")
                )
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            image_generation = envelope.get("image_generation")
            if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
                aspect_ratio = image_generation.get("aspect_ratio")
                metadata["image_generation"] = {
                    "enabled": True,
                    "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
                }
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            if (
                msg.metadata.get("_progress")
                or msg.metadata.get("_file_edit_events")
                or msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
                or msg.metadata.get("_goal_state_sync")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
            return
        if msg.metadata.get("_goal_state_sync"):
            blob = msg.metadata.get("goal_state")
            await self.send_goal_state(msg.chat_id, blob if isinstance(blob, dict) else {"active": False})
            return
        if msg.metadata.get("_goal_status"):
            status = msg.metadata.get("goal_status")
            if status in ("running", "idle"):
                started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                await self.send_goal_status(
                    msg.chat_id,
                    status,
                    started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                )
            return
        # Signal that the agent has fully finished processing the current turn.
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            await self.send_turn_end(msg.chat_id, latency_ms=lat_i, goal_state=gs_blob)
            return
        if msg.metadata.get("_session_updated"):
            scope = msg.metadata.get("_session_update_scope")
            await self.send_session_updated(
                msg.chat_id,
                scope=scope if isinstance(scope, str) else None,
            )
            return
        if msg.metadata.get("_file_edit_events"):
            payload: dict[str, Any] = {
                "event": "file_edit",
                "chat_id": msg.chat_id,
                "edits": msg.metadata["_file_edit_events"],
            }
            self._try_append_webui_transcript(msg.chat_id, payload)
            raw = json.dumps(payload, ensure_ascii=False)
            for connection in conns:
                await self._safe_send_to(connection, raw, label=" ")
            return
        text = msg.content
        wire_text = self._rewrite_local_markdown_images(text)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": wire_text,
        }
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        transcript_payload = dict(payload)
        transcript_payload["text"] = text
        self._try_append_webui_transcript(msg.chat_id, transcript_payload)
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns or not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        stream_key = (chat_id, str(meta.get("_stream_id") or ""))
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = self._stream_text_buffers.pop(stream_key, [])
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            rewritten = self._rewrite_local_markdown_images(full_text)
            if rewritten != full_text:
                body["text"] = rewritten
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """Notify clients that session metadata changed outside the main turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
