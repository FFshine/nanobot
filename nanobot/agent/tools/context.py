"""Runtime context for tool construction."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

_current_workspace: ContextVar[Path | None] = ContextVar("nanobot_workspace", default=None)


def current_workspace() -> Path | None:
    """Return the per-turn workspace override, or None."""
    return _current_workspace.get()


def bind_workspace(workspace: Path) -> None:
    """Set the per-turn workspace for all tools in this async task."""
    _current_workspace.set(workspace)


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


@dataclass
class ToolContext:
    config: Any
    workspace: str
    bus: Any | None = None
    subagent_manager: Any | None = None
    cron_service: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    image_generation_provider_configs: dict[str, Any] | None = None
    timezone: str = "UTC"
