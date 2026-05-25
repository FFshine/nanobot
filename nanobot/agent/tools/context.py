"""Runtime context for tool construction."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

_current_workspace: ContextVar[Path | None] = ContextVar("nanobot_workspace", default=None)
_current_user_role: ContextVar[str] = ContextVar("nanobot_user_role", default="")
_current_group_workspaces: ContextVar[list[Path]] = ContextVar(
    "nanobot_group_workspaces", default=[]
)
_current_effective_disabled_skills: ContextVar[set[str]] = ContextVar(
    "nanobot_effective_disabled_skills", default=set()
)


def current_workspace() -> Path | None:
    """Return the per-turn workspace override, or None."""
    return _current_workspace.get()


def bind_workspace(workspace: Path) -> None:
    """Set the per-turn workspace for all tools in this async task."""
    _current_workspace.set(workspace)


def current_user_role() -> str:
    """Return the per-turn user role, or empty string if not set."""
    return _current_user_role.get()


def bind_user_role(role: str) -> Token:
    """Set the per-turn user role for all tools in this async task.

    Returns a Token that can be passed to ``_current_user_role.reset()``
    to restore the previous value.
    """
    return _current_user_role.set(role)


def current_group_workspaces() -> list[Path]:
    """Return the per-turn group workspace paths, or empty list."""
    return _current_group_workspaces.get()


def bind_group_workspaces(workspaces: list[Path]) -> None:
    """Set the per-turn group workspaces for skill loading."""
    _current_group_workspaces.set(workspaces)


def current_effective_disabled_skills() -> set[str]:
    """Return the per-turn effective disabled skills (user + group merged)."""
    return _current_effective_disabled_skills.get()


def bind_effective_disabled_skills(skills: set[str]) -> None:
    """Set the per-turn effective disabled skills."""
    _current_effective_disabled_skills.set(skills)


def is_workspace_restricted_for_user(config_restrict: bool) -> bool:
    """Return whether workspace restriction should be enforced.

    Non-admin users are always restricted regardless of the global config
    flag. Admins and unauthenticated sessions (legacy single-user mode)
    respect the config flag.
    """
    role = current_user_role()
    if role and role != "admin":
        return True
    return config_restrict


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
    cron_resolver: Callable[[], Any] | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    image_generation_provider_configs: dict[str, Any] | None = None
    timezone: str = "UTC"
