"""User model."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    display_name: str = ""
    role: str = "user"
    is_active: bool = True
    settings: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "is_active": self.is_active,
            "settings": self.settings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "displayName": self.display_name,
            "role": self.role,
            "settings": self.settings,
        }

    @classmethod
    def from_row(cls, row: object) -> User:
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        else:
            d = dict(row)
        d["is_active"] = bool(d.pop("is_active", True))
        d["settings"] = (
            json.loads(d["settings"]) if isinstance(d.get("settings"), str) else d.get("settings", {})
        )
        return cls(**d)


@dataclass
class Group:
    id: str
    name: str
    display_name: str = ""
    settings: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "displayName": self.display_name,
            "settings": self.settings,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: object) -> Group:
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        else:
            d = dict(row)
        d["settings"] = (
            json.loads(d["settings"]) if isinstance(d.get("settings"), str) else d.get("settings", {})
        )
        return cls(**d)


@dataclass
class GroupMember:
    group_id: str
    user_id: str
    role: str = "member"  # "admin" | "member"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {
            "groupId": self.group_id,
            "userId": self.user_id,
            "role": self.role,
        }

    @classmethod
    def from_row(cls, row: object) -> GroupMember:
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        else:
            d = dict(row)
        return cls(**d)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
