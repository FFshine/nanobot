---
name: group-admin
description: Manage group-level skills for nanobot groups. Use when the user wants to create, edit, delete, or list skills shared across a group, or when they ask "add a skill to my group", "create a shared skill for engineering", "list group skills", or similar group-skill operations.
---

# Group Admin — Managing Group Skills

## Directory Layout

```
~/.nanobot/workspaces/groups/{group_id}/
└── skills/
    └── {skill-name}/
        └── SKILL.md
```

- `{group_id}` is the group's UUID (visible in `get_user_groups()` output).
- Skill names use lowercase, digits, and hyphens only.
- Group skills are shared by all members of that group.

## Permission Model

- Only group members with role `"admin"` can create/edit/delete group skills.
- The agent must verify admin membership before touching any group skill file.
- Group skills have priority: **user skills > group skills > builtin skills**.

## Step 1 — Find Groups Where the User Is Admin

The user's database ID must be determined first. Use the approach that matches the situation.

### Approach A — Parse workspace path (most reliable)

The system prompt shows a `Workspace Folder` line. For authenticated users the path ends with the database user ID:

```
Workspace Folder: /home/user/.nanobot/workspaces/{user_id}
```

Extract the last path segment as the user ID. If it is `cli` (the unauthenticated workspace), the user needs to log in first — tell them so and stop.

### Approach B — Look up by username

If the user tells you their username (but not their UUID), use:

```bash
python -c "
from nanobot.auth import get_user_by_username
u = get_user_by_username('USERNAME')
print(u.id if u else 'NOT FOUND')
"
```

### Approach C — Sender ID (only if the sender ID looks like a UUID, not `anon-...`)

The runtime context at the bottom of each turn may include `Sender ID: <id>`. Only use this if it looks like a 32-char hex UUID. If it starts with `anon-`, use Approach A or B instead.

### List admin groups

Once you have the correct user_id, run:

```bash
python -c "
from nanobot.auth import get_user_groups, get_group_members
user_id = 'USER_ID'
for g in get_user_groups(user_id):
    for m in get_group_members(g.id):
        if m.user_id == user_id and m.role == 'admin':
            print(f'{g.id}  {g.name}  display_name={g.display_name}')
            break
"
```

Replace `USER_ID` with the actual user ID.

If the output is empty, tell the user they don't have admin access to any group and stop.

## Step 2 — Ask Which Group

Show the user the list and ask: "Which group should this skill be created in?"

Let them pick one. Confirm the `group_id` before proceeding.

## Step 3 — Determine the Skill Path

The group workspace root is:

```
~/.nanobot/workspaces/groups/{group_id}/
```

The skill directory is:

```
~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/
```

The skill file is:

```
~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/SKILL.md
```

## Step 4 — Create / Edit / Delete

### Create a new skill

```bash
mkdir -p ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}
```

Then use `write_file` to write the `SKILL.md`. Follow the skill format from the `skill-creator` skill:

```markdown
---
name: {skill-name}
description: {one-line summary of what this skill does and when to use it}
---

# {Title}

{Markdown body with instructions}
```

### List group skills

```bash
ls ~/.nanobot/workspaces/groups/{group_id}/skills/
```

Or via the API (requires auth token):

```
GET /api/groups/{group_id}/skills
```

### View a skill

```bash
cat ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/SKILL.md
```

Or via API:

```
GET /api/groups/{group_id}/skills/{skill-name}/content
```

### Edit a skill

Use `read_file` to read the current content, then `write_file` to update.

### Delete a skill

```bash
rm -rf ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}
```

Or via API:

```
GET /api/groups/{group_id}/skills/{skill-name}/delete
```

## Step 5 — Confirm

After creating/editing/deleting a group skill, tell the user:
- What was done
- Which group it affects
- That all group members will have access to the skill on their next turn

## Constraints

- Skill name: lowercase, digits, hyphens only, under 64 characters.
- The `SKILL.md` must have valid YAML frontmatter with at least `name` and `description`.
- Never create extraneous files (README.md, CHANGELOG.md, etc.) — only SKILL.md per skill directory.
- Group skills are immediately available to all group members after creation.
