"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanobot.utils.helpers import ensure_dir


def get_config_path() -> Path:
    """Get the configuration file path (lazy import to break circular dependency).

    Delegates to ``nanobot.config.loader.get_config_path`` at call time so
    that importing this module never triggers a circular import during startup.
    """
    from nanobot.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None, user_id: str = "") -> Path:
    """Return the media directory, optionally namespaced per channel and user."""
    base = get_runtime_subdir("media")
    if channel:
        base = ensure_dir(base / channel)
        if user_id:
            base = ensure_dir(base / user_id)
    return base


def get_cron_dir(user_id: str = "") -> Path:
    """Return the cron storage directory.

    When *user_id* is provided, the path is scoped to the per-user
    workspace so that scheduled jobs are isolated.
    """
    if user_id:
        return ensure_dir(get_workspace_path(user_id=user_id) / "cron")
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_webui_dir(user_id: str = "") -> Path:
    """Return the directory for WebUI-only persisted display threads (JSON).

    When *user_id* is provided, the path is scoped to the per-user
    workspace so that sidebar state, threads, and transcripts are isolated.
    """
    if user_id and user_id != "__legacy__":
        return ensure_dir(get_workspace_path(user_id=user_id) / "webui")
    return get_runtime_subdir("webui")


CLI_WORKSPACE = Path.home() / ".nanobot" / "workspaces" / "cli"


def get_workspace_path(workspace: str | None = None, user_id: str = "") -> Path:
    """Resolve and ensure the agent workspace path.

    When *user_id* is provided, the workspace is scoped to
    ``~/.nanobot/workspaces/users/{user_id}`` regardless of any explicit
    workspace override.  This keeps per-user data isolated.

    Without a user_id, CLI/unauthenticated sessions use
    ``~/.nanobot/workspaces/cli``.
    """
    if user_id and user_id != "__legacy__":
        return ensure_dir(Path.home() / ".nanobot" / "workspaces" / "users" / user_id)
    if workspace:
        return ensure_dir(Path(workspace).expanduser())
    return ensure_dir(CLI_WORKSPACE)


def is_default_workspace(workspace: str | Path | None, user_id: str = "") -> bool:
    """Return whether a workspace resolves to the default CLI workspace path."""
    if user_id and user_id != "__legacy__":
        return True
    current = Path(workspace).expanduser() if workspace is not None else CLI_WORKSPACE
    return current.resolve(strict=False) == CLI_WORKSPACE.resolve(strict=False)


def get_group_workspace_path(group_id: str) -> Path:
    """Return the workspace path for a group.

    Group workspaces live under ``~/.nanobot/workspaces/groups/{group_id}/``
    and hold group-level skills and settings shared by all group members.
    """
    return ensure_dir(Path.home() / ".nanobot" / "workspaces" / "groups" / group_id)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return Path.home() / ".nanobot" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanobot" / "sessions"
