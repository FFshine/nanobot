"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

import yaml

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


def link_builtin_skills(
    workspace: Path, builtin_skills_dir: Path | None = None
) -> None:
    """Symlink builtin skill dirs into ``{workspace}/skills/``.

    Restricted users can only access paths inside their workspace, so
    builtin skills must appear within that boundary.  Symlinks are created
    only when the user does not already have a skill directory with the
    same name (user skills shadow builtin ones).

    Safe to call multiple times — existing links and user dirs are
    silently skipped.
    """
    src = builtin_skills_dir or BUILTIN_SKILLS_DIR
    if not src.is_dir():
        return
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for builtin_dir in src.iterdir():
        if not builtin_dir.is_dir():
            continue
        link_path = skills_dir / builtin_dir.name
        if link_path.exists():
            continue
        link_path.symlink_to(builtin_dir)


def link_group_skills(workspace: Path, group_workspaces: list[Path]) -> None:
    """Symlink group skill dirs into ``{workspace}/skills/``.

    Like ``link_builtin_skills``, this ensures group skills are accessible
    within the user's workspace boundary so the ``read_file`` tool can
    read them.  User skills shadow group skills, and symlinks are skipped
    when a directory with the same name already exists.

    Safe to call multiple times — existing links and user dirs are
    silently skipped.
    """
    if not group_workspaces:
        return
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for gws in group_workspaces:
        gws_skills = gws / "skills"
        if not gws_skills.is_dir():
            continue
        for skill_dir in gws_skills.iterdir():
            if not skill_dir.is_dir():
                continue
            link_path = skills_dir / skill_dir.name
            if link_path.exists():
                continue
            link_path.symlink_to(skill_dir)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()

    @property
    def _effective_workspace_skills(self) -> Path:
        """Return the per-user workspace skills dir when available, else the global one."""
        from nanobot.agent.tools.context import current_workspace

        if (user_ws := current_workspace()) is not None:
            return user_ws / "skills"
        return self.workspace_skills

    @property
    def _effective_disabled_skills(self) -> set[str]:
        """Return per-turn disabled skills when bound, else the init-time set."""
        from nanobot.agent.tools.context import current_effective_disabled_skills

        effective = current_effective_disabled_skills()
        if effective:
            return effective
        return self.disabled_skills

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source', 'description',
            'emoji', 'always'.
        """
        # Ensure builtin symlinks are current so new builtin skills appear
        # in existing workspaces (idempotent — skips paths that already exist).
        if self.builtin_skills and self.builtin_skills.exists():
            link_builtin_skills(self.workspace, builtin_skills_dir=self.builtin_skills)

        # Collect builtin names for symlink detection below.
        builtin_names: set[str] = set()
        if self.builtin_skills and self.builtin_skills.exists():
            builtin_names = {
                d.name
                for d in self.builtin_skills.iterdir()
                if d.is_dir() and (d / "SKILL.md").exists()
            }

        # Scan workspace skills.  Symlinks pointing to builtin or group
        # skill dirs are separated out so they get the correct source label
        # (no delete button in UI for builtin/group skills).
        all_workspace = self._skill_entries_from_dir(self._effective_workspace_skills, "user")
        user_skills: list[dict[str, str]] = []
        group_skills: list[dict[str, str]] = []
        for entry in all_workspace:
            skill_dir = self._effective_workspace_skills / entry["name"]
            if skill_dir.is_symlink():
                if entry["name"] in builtin_names:
                    continue  # handled by builtin pass below
                target = os.readlink(skill_dir)
                if "/workspaces/groups/" in target:
                    entry["source"] = "group"
                    group_skills.append(entry)
                    continue
            user_skills.append(entry)

        skills = user_skills + group_skills
        workspace_names = {entry["name"] for entry in skills}

        # Builtin skills — shadowed by user and group skills
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        disabled = self._effective_disabled_skills
        if disabled:
            skills = [s for s in skills if s["name"] not in disabled]

        # Enrich entries with metadata (description, emoji, always flag)
        for entry in skills:
            meta = self.get_skill_metadata(entry["name"]) or {}
            entry["description"] = meta.get("description", "")
            nb = self._parse_nanobot_metadata(meta.get("metadata"))
            entry["emoji"] = nb.get("emoji", "")
            entry["always"] = bool(meta.get("always") or nb.get("always"))

        if filter_unavailable:
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # User/group skills first (symlinks bring group skills into workspace)
        roots: list[Path] = [self._effective_workspace_skills]
        # Builtin skills last
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """
        Build a summary of all skills grouped by source (builtin / user).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Args:
            exclude: Set of skill names to omit from the summary.

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        builtin: list[dict[str, str]] = []
        group: list[dict[str, str]] = []
        user: list[dict[str, str]] = []
        for entry in all_skills:
            src = entry.get("source", "")
            if src == "builtin":
                builtin.append(entry)
            elif src == "group":
                group.append(entry)
            else:
                user.append(entry)

        def _format_block(skills: list[dict[str, str]], heading: str) -> str:
            lines: list[str] = [f"### {heading}"]
            for entry in skills:
                skill_name = entry["name"]
                if exclude and skill_name in exclude:
                    continue
                meta = self._get_skill_meta(skill_name)
                available = self._check_requirements(meta)
                desc = self._get_skill_description(skill_name)
                if available:
                    lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
                else:
                    missing = self._get_missing_requirements(meta)
                    suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                    lines.append(f"- **{skill_name}** — {desc}{suffix}  `{entry['path']}`")
            return "\n".join(lines)

        blocks: list[str] = []
        if builtin:
            blocks.append(_format_block(builtin, "Builtin Skills"))
        if group:
            blocks.append(_format_block(group, "Group Skills"))
        if user:
            blocks.append(_format_block(user, "User Skills"))
        return "\n\n".join(blocks)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: object) -> dict:
        """Extract nanobot/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("nanobot", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_nanobot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata
