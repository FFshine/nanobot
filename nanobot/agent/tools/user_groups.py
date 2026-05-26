"""Tool for listing the current user's group memberships.

Used by the group-admin skill to discover which groups the user can administer,
and by the agent when it needs to know the user's group context.
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import current_user_id


class ListUserGroupsTool(Tool):
    """List groups the current user belongs to, with membership roles."""

    _plugin_discoverable = True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "list_user_groups"

    @property
    def description(self) -> str:
        return (
            "List all groups the current user belongs to. "
            "Returns group id, name, display name, and the user's role (admin or member) for each group. "
            "Use this to discover which groups you can manage via the group-admin skill, "
            "or to check your group memberships."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        user_id = current_user_id()
        if not user_id:
            return json.dumps({
                "error": "User ID not available",
                "hint": "The user_id context is not set. This tool requires an authenticated session.",
            })

        from nanobot.auth import get_group_members, get_user_groups

        try:
            groups = get_user_groups(user_id)
        except Exception as e:
            return json.dumps({"error": f"Failed to fetch user groups: {e}"})

        result = []
        for g in groups:
            role = "member"
            try:
                for m in get_group_members(g.id):
                    if m.user_id == user_id:
                        role = m.role
                        break
            except Exception:
                pass
            result.append({
                "id": g.id,
                "name": g.name,
                "displayName": g.display_name,
                "role": role,
            })

        return json.dumps({"groups": result})
