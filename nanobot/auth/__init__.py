"""Authentication and user management for nanobot."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from nanobot.auth.db import _new_id, get_db
from nanobot.auth.models import Group, GroupMember, User
from nanobot.auth.password import hash_password, verify_password

__all__ = [
    "User",
    "Group",
    "GroupMember",
    "hash_password",
    "verify_password",
    "create_user",
    "authenticate_user",
    "get_user_by_id",
    "get_user_by_username",
    "list_users",
    "update_user",
    "delete_user",
    "get_user_count",
    # Group management
    "create_group",
    "get_group",
    "get_group_by_name",
    "list_groups",
    "update_group",
    "delete_group",
    # Group membership
    "add_group_member",
    "remove_group_member",
    "get_group_members",
    "get_user_groups",
    "get_user_group_ids",
]


def create_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
    role: str = "user",
    settings: dict | None = None,
) -> User:
    db = get_db()
    now = _utc_now()
    user_id = _new_id()
    pw_hash = hash_password(password)
    settings_json = json.dumps(settings or {})
    try:
        db.execute(
            "INSERT INTO users (id, username, password_hash, display_name, role, is_active, settings, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (user_id, username, pw_hash, display_name, role, settings_json, now, now),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return User(
        id=user_id,
        username=username,
        password_hash=pw_hash,
        display_name=display_name,
        role=role,
        is_active=True,
        settings=settings or {},
        created_at=now,
        updated_at=now,
    )


def authenticate_user(username: str, password: str) -> User | None:
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
    ).fetchone()
    if row is None:
        return None
    user = User.from_row(row)
    if not verify_password(password, user.password_hash):
        return None
    return user


def get_user_by_id(user_id: str) -> User | None:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.from_row(row) if row else None


def get_user_by_username(username: str) -> User | None:
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return User.from_row(row) if row else None


def list_users() -> list[User]:
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [User.from_row(r) for r in rows]


def update_user(
    user_id: str,
    *,
    display_name: str | None = None,
    password: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    settings: dict | None = None,
) -> User | None:
    db = get_db()
    user = get_user_by_id(user_id)
    if user is None:
        return None
    now = _utc_now()
    if display_name is not None:
        db.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?", (display_name, now, user_id))
    if password is not None:
        db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (hash_password(password), now, user_id))
    if role is not None:
        db.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, now, user_id))
    if is_active is not None:
        db.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?", (int(is_active), now, user_id))
    if settings is not None:
        db.execute("UPDATE users SET settings = ?, updated_at = ? WHERE id = ?", (json.dumps(settings), now, user_id))
    db.commit()
    return get_user_by_id(user_id)


def delete_user(user_id: str) -> bool:
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return True


def get_user_count() -> int:
    db = get_db()
    row = db.execute("SELECT COUNT(*) as c FROM users").fetchone()
    return row["c"] if row else 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------


def create_group(
    name: str,
    *,
    display_name: str = "",
    settings: dict | None = None,
) -> Group:
    db = get_db()
    now = _utc_now()
    group_id = _new_id()
    settings_json = json.dumps(settings or {})
    try:
        db.execute(
            "INSERT INTO groups_ (id, name, display_name, settings, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, name, display_name, settings_json, now, now),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Group(
        id=group_id, name=name, display_name=display_name,
        settings=settings or {}, created_at=now, updated_at=now,
    )


def get_group(group_id: str) -> Group | None:
    db = get_db()
    row = db.execute("SELECT * FROM groups_ WHERE id = ?", (group_id,)).fetchone()
    return Group.from_row(row) if row else None


def get_group_by_name(name: str) -> Group | None:
    db = get_db()
    row = db.execute("SELECT * FROM groups_ WHERE name = ?", (name,)).fetchone()
    return Group.from_row(row) if row else None


def list_groups() -> list[Group]:
    db = get_db()
    rows = db.execute("SELECT * FROM groups_ ORDER BY created_at").fetchall()
    return [Group.from_row(r) for r in rows]


def update_group(
    group_id: str,
    *,
    display_name: str | None = None,
    settings: dict | None = None,
) -> Group | None:
    db = get_db()
    group = get_group(group_id)
    if group is None:
        return None
    now = _utc_now()
    if display_name is not None:
        db.execute(
            "UPDATE groups_ SET display_name = ?, updated_at = ? WHERE id = ?",
            (display_name, now, group_id),
        )
    if settings is not None:
        db.execute(
            "UPDATE groups_ SET settings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(settings), now, group_id),
        )
    db.commit()
    return get_group(group_id)


def delete_group(group_id: str) -> bool:
    db = get_db()
    row = db.execute("SELECT id FROM groups_ WHERE id = ?", (group_id,)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM groups_ WHERE id = ?", (group_id,))
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Group membership
# ---------------------------------------------------------------------------


def add_group_member(group_id: str, user_id: str, *, role: str = "member") -> GroupMember:
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
        (group_id, user_id, role),
    )
    db.commit()
    return GroupMember(group_id=group_id, user_id=user_id, role=role)


def remove_group_member(group_id: str, user_id: str) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    if not row:
        return False
    db.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    db.commit()
    return True


def get_group_members(group_id: str) -> list[GroupMember]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM group_members WHERE group_id = ?", (group_id,)
    ).fetchall()
    return [GroupMember.from_row(r) for r in rows]


def get_user_groups(user_id: str) -> list[Group]:
    db = get_db()
    rows = db.execute(
        "SELECT g.* FROM groups_ g "
        "INNER JOIN group_members gm ON gm.group_id = g.id "
        "WHERE gm.user_id = ? ORDER BY g.created_at",
        (user_id,),
    ).fetchall()
    return [Group.from_row(r) for r in rows]


def get_user_group_ids(user_id: str) -> list[str]:
    db = get_db()
    rows = db.execute(
        "SELECT group_id FROM group_members WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return [r["group_id"] for r in rows]
