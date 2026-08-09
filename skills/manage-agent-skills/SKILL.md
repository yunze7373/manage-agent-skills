---
name: manage-agent-skills
description: Manage Agent Skills across Codex, Claude Code, Copilot CLI, OpenClaw, and Hermes. Audit, search, enable, disable, set Claude Code visibility states, and apply presets without deleting files. Use when the user wants to reduce idle skill context, keep rarely used skills manual, inspect newly installed skills on demand, or toggle skills for one specific agent.
---

# Manage Agent Skills

Use the bundled `scripts/skillctl.py` for deterministic, on-demand management.
Never start a daemon or watcher. Discover current skills again on every invocation.

## Workflow

1. Run `doctor` or `status` before changing anything.
2. Use `search` to resolve exact skill names.
3. Preview mutations with `--dry-run`.
4. Apply the requested mutation.
5. Report the platform, affected skills, config or native command used, backup path,
   and whether a new agent session is required.

Choose the Python launcher available on the current operating system (`python`
on Windows, commonly `python3` on macOS/Linux) and resolve the skill directory
before running the script.

Windows PowerShell:

```powershell
$skillctl = "<skill-directory>\scripts\skillctl.py"
python "$skillctl" doctor
python "$skillctl" --platform codex status
python "$skillctl" --platform all search azure
```

macOS/Linux:

```bash
skillctl="<skill-directory>/scripts/skillctl.py"
python3 "$skillctl" doctor
python3 "$skillctl" --platform codex status
python3 "$skillctl" --platform all search azure
```

Mutation commands use the same arguments on every operating system:

```text
<python> <skillctl> --platform claude disable deploy --dry-run
<python> <skillctl> --platform claude disable deploy
<python> <skillctl> --platform copilot enable my-skill --dry-run
<python> <skillctl> --platform openclaw disable browser-tools --dry-run
<python> <skillctl> --platform hermes --hermes-scope telegram disable shell-tools --dry-run
```

For automation, pass `--json` either before or after the command. Use the
format that keeps the caller's command template readable:

```text
<python> <skillctl> --platform all --json status
<python> <skillctl> --platform all status --json
```

Mutations require an explicit `--platform codex`, `--platform claude`, or
`--platform copilot`, `--platform openclaw`, or `--platform hermes`. Selectors
are exact names, `group:<name>`, `path:<path>`, or `all`; partial names are
intentionally rejected. Hermes defaults to global scope; pass
`--hermes-scope <platform>` for a platform-specific disable.

A selector matches either the display name or the identifier the host's own
configuration keys on. When they differ, `search` prints the identifier as
`(key=...)`; prefer that value.

Groups are discovery sources only (`shared`, `claude-user`, a plugin name).
No skill is grouped by name unless the user supplies a taxonomy:

```text
<python> <skillctl> --platform codex --groups my-groups.json disable group:azure --dry-run
```

The file is `{"version": 1, "groups": {"azure": ["azure-*"]}}`; patterns are
`fnmatch` style. Never assume a group exists — confirm it with `search` first.

## Claude Code visibility states

Claude Code has four states rather than two. Use `set` to reach the middle
ones, and prefer `name-only` when the goal is to cut idle context while
keeping the skill discoverable:

```text
<python> <skillctl> --platform claude set name-only legacy-context
<python> <skillctl> --platform claude set user-invocable-only deploy
<python> <skillctl> --platform claude set off legacy-context --dry-run
```

| State | Listed to Claude | In `/` menu |
| --- | --- | --- |
| `on` | Name and description | Yes |
| `name-only` | Name only | Yes |
| `user-invocable-only` | Hidden | Yes |
| `off` | Hidden | Hidden |

`enable` and `disable` remain shorthand for `on` and `off`. The other
platforms accept only `enable` and `disable`.

Claude Code keys personal and project skills by **directory name**; the
frontmatter `name` is only the label shown in listings. The manager writes the
directory name and reports `display_name_differs` when the two disagree.

For `path:` selectors, use the native absolute path printed by `search`, and
quote the entire selector when it contains spaces:

```text
Windows: path:C:\skills\demo\SKILL.md
macOS:   path:/Users/alice/.agents/skills/demo/SKILL.md
Linux:   path:/home/alice/.agents/skills/demo/SKILL.md
```

Do not translate paths between operating systems; matching uses the path rules
of the system running `skillctl.py`.

## Presets

Use a JSON preset file matching `references/presets.md`:

```text
<python> <skillctl> --platform codex preset lean --file skill-presets.json --dry-run
<python> <skillctl> --platform codex preset lean --file skill-presets.json
```

## Safety

- Do not delete, relocate, or rewrite any installed `SKILL.md`.
- Keep changes scoped to native platform controls.
- Codex edits only its marked TOML block.
- Claude Code edits only `skillOverrides` in its settings JSON. The manager
  currently reads and writes the user-level file only; a project or local
  settings file can still override the result.
- Copilot writes the documented `disabledSkills` setting and uses
  `copilot skill list --json` for native discovery when available.
- OpenClaw delegates mutations to `openclaw config set` so its JSON5 config is
  not normalized or hand-edited. Agent allowlists may still restrict a skill.
- Hermes writes only `skills.disabled` or
  `skills.platform_disabled.<scope>` in `config.yaml`. PyYAML may normalize
  formatting and comments, so the backup is the lossless recovery copy.
- Create a sibling `.manage-agent-skills.bak` before changing a config file.
- Protect `manage-agent-skills`, `skill-creator`, and `plugin-creator` from
  disable-all unless the user explicitly requests `--force`.
- Explain that Codex, Claude Code, OpenClaw, and Hermes changes may require a
  new session or gateway restart; Copilot follows its native command behavior.

Read `references/platforms.md` when platform semantics or support boundaries
matter. Read `references/presets.md` only when creating or applying presets.
