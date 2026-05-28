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

    def _skill_entries_from_dir(
        self,
        base: Path,
        source: str,
        *,
        skip_names: set[str] | None = None,
        group_id: str = "",
        group_name: str = "",
    ) -> list[dict[str, str]]:
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
            entry: dict[str, str] = {"name": name, "path": str(skill_file), "source": source}
            if group_id:
                entry["group_id"] = group_id
            if group_name:
                entry["group_name"] = group_name
            entries.append(entry)
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
        # 1. User skills from the effective workspace skills dir.
        # Real user skills are always directories; skip symlinks (stale
        # leftovers from a previous link_builtin_skills / link_group_skills).
        user_skills = [
            e
            for e in self._skill_entries_from_dir(self._effective_workspace_skills, "user")
            if not (self._effective_workspace_skills / e["name"]).is_symlink()
        ]
        used_names = {entry["name"] for entry in user_skills}

        # 2. Group skills from per-turn group workspace bindings.
        from nanobot.agent.tools.context import current_group_workspaces

        group_skills: list[dict[str, str]] = []
        for gws in current_group_workspaces():
            gws_skills = gws / "skills"
            gid = gws.name
            gname = gid
            try:
                from nanobot.auth import get_group

                g = get_group(gid)
                if g is not None:
                    gname = g.display_name or g.name or gid
            except Exception:
                pass
            entries = self._skill_entries_from_dir(
                gws_skills, "group", skip_names=used_names, group_id=gid, group_name=gname,
            )
            group_skills.extend(entries)
            used_names.update(entry["name"] for entry in entries)

        skills = user_skills + group_skills

        # 3. Builtin skills — shadowed by user and group skills
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=used_names)
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
        # User skills first, then group, then builtin (user > group > builtin).
        roots: list[Path] = [self._effective_workspace_skills]

        from nanobot.agent.tools.context import current_group_workspaces

        for gws in current_group_workspaces():
            roots.append(gws / "skills")
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
        group_by_name: dict[str, list[dict[str, str]]] = {}
        user: list[dict[str, str]] = []
        for entry in all_skills:
            src = entry.get("source", "")
            if src == "builtin":
                builtin.append(entry)
            elif src == "group":
                gname = entry.get("group_name", "Group")
                group_by_name.setdefault(gname, []).append(entry)
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
        for gname, gskills in sorted(group_by_name.items()):
            blocks.append(_format_block(gskills, f"Group Skills ({gname})"))
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
