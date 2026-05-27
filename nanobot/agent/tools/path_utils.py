"""Shared path helpers for workspace-scoped tools."""

import os
from pathlib import Path

from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.context import current_workspace
from nanobot.config.paths import get_media_dir

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)

# Directories that must never be written to by any agent tool.
_NO_WRITE_DIRS: tuple[Path, ...] = (BUILTIN_SKILLS_DIR.resolve(),)


def is_under(path: Path, directory: Path) -> bool:
    """Return True when path resolves under directory."""
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def is_path_safe(path_str: str, allowed_dirs: list[Path]) -> tuple[bool, str]:
    """Check whether *path_str* resolves under one of *allowed_dirs*.

    Returns ``(True, "")`` when safe, ``(False, reason)`` when it escapes.
    Handles ``~`` expansion, environment variables, and symlink resolution.
    """
    try:
        p = Path(path_str).expanduser()
        # Expand environment variables so $HOME/../etc is resolved correctly.
        expanded = os.path.expandvars(str(p))
        resolved = Path(expanded).resolve()
    except Exception as e:
        return False, f"cannot resolve path: {e}"

    for d in allowed_dirs:
        try:
            resolved.relative_to(d.resolve())
            return True, ""
        except ValueError:
            pass
        # Also allow the directory itself (not just children).
        try:
            if resolved == d.resolve():
                return True, ""
        except Exception:
            pass

    return False, "path resolves outside allowed directories"


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    *,
    for_write: bool = False,
) -> Path:
    """Resolve path against workspace and enforce allowed directory containment."""
    # Per-user workspace override (set at turn time via bind_workspace).
    if (user_ws := current_workspace()) is not None:
        workspace = user_ws
        if allowed_dir is not None:
            allowed_dir = user_ws
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        media_path = get_media_dir().resolve()
        all_dirs = [allowed_dir, media_path, *(extra_allowed_dirs or [])]
        if not any(is_under(resolved, d) for d in all_dirs):
            raise PermissionError(
                f"Path {path} is outside allowed directory {allowed_dir}"
                + WORKSPACE_BOUNDARY_NOTE
            )
    if for_write:
        for blocked in _NO_WRITE_DIRS:
            if is_under(resolved, blocked):
                raise PermissionError(
                    f"Path {path} is inside a protected system directory {blocked}"
                    + WORKSPACE_BOUNDARY_NOTE
                )
    return resolved
