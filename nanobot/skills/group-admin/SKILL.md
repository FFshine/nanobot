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
        ├── SKILL.md          (required)
        ├── scripts/          (optional — Python/Bash scripts)
        ├── references/       (optional — docs loaded at runtime)
        └── assets/           (optional — templates, images, etc.)
```

- `{group_id}` is the group's UUID (visible via the `list_user_groups` tool).
- Skill names use lowercase, digits, and hyphens only.
- Group skills are shared by all members of that group.
- **All skill files (scripts, assets, references) MUST be placed inside the group skill directory.** Never put skill dependencies in your personal workspace — other group members cannot access them.

## Permission Model

- Only group members with role `"admin"` can create/edit/delete group skills.
- The agent must verify admin membership before touching any group skill file.
- Group skills have priority: **user skills > group skills > builtin skills**.

## Step 1 — Find Your User ID

The system prompt shows a `Workspace Folder` line. For authenticated users the path ends with the database user ID:

```
Workspace Folder: /home/user/.nanobot/workspaces/{user_id}
```

Extract the last path segment as your user ID. If it is `cli` (the unauthenticated workspace), you need to log in first — tell the user so and stop.

**Fallback**: The runtime context at the bottom of each turn may include `Sender ID: <id>`. Only use this if it looks like a 32-char hex UUID. If it starts with `anon-`, rely on the Workspace Folder path instead.

## Step 2 — List Groups Where You Are Admin

Use the `list_user_groups` tool to get your group memberships. The tool returns JSON with each group's id, name, displayName, and your role (admin or member).

Filter for groups where `"role"` is `"admin"`. If none, tell the user you don't have admin access to any group and stop.

## Step 3 — Ask Which Group

Show the user the admin groups and ask: "Which group should this skill be created in?"

Let them pick one. Confirm the `group_id` before proceeding.

## Step 4 — Determine the Skill Path

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

## Step 5 — Create / Edit / Delete

### CRITICAL: All skill files MUST live in the group directory

When a skill needs bundled resources (scripts, references, assets, templates, etc.), place them **inside the group skill directory** — NOT in your personal workspace. Every file the skill depends on must be accessible to all group members via the shared group path.

```
~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/
├── SKILL.md
├── scripts/        # Executable scripts the skill needs
├── references/     # Reference docs loaded at runtime
└── assets/         # Templates, fonts, images, etc.
```

**Wrong**: writing scripts/templates to your personal workspace (`~/.nanobot/workspaces/{user_id}/skills/...`). Only you can access those files.
**Correct**: writing everything under `~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/`.

### Create a new skill

First create the skill directory and any resource subdirectories:

```bash
mkdir -p ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}
```

If the skill needs bundled resources, also create the relevant subdirectories:

```bash
mkdir -p ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/scripts
mkdir -p ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/assets
mkdir -p ~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/references
```

Then use `write_file` to write the `SKILL.md` and any bundled resource files, always using the group path prefix `~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/`. Follow the skill format from the `skill-creator` skill:

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

## Step 6 — Confirm

After creating/editing/deleting a group skill, tell the user:
- What was done
- Which group it affects
- That all group members will have access to the skill on their next turn

## Constraints

- Skill name: lowercase, digits, hyphens only, under 64 characters.
- The `SKILL.md` must have valid YAML frontmatter with at least `name` and `description`.
- Never create extraneous files (README.md, CHANGELOG.md, etc.) — only SKILL.md per skill directory.
- Group skills are immediately available to all group members after creation.
- **CRITICAL**: Place ALL skill files under the group path (`~/.nanobot/workspaces/groups/{group_id}/skills/{skill-name}/`). Never put scripts, assets, or references in your personal workspace — group members cannot access files outside the group directory.
