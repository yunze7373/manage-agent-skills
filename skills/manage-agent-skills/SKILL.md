---
name: manage-agent-skills
description: Audit, search, enable, disable, and apply presets to installed Agent Skills without deleting their files. Use when the user wants to reduce idle skill context, keep rarely used skills manual, inspect newly installed skills on demand, or manage skills across Codex, Claude Code, GitHub Copilot CLI, OpenClaw, and Hermes Agent.
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

```powershell
$skillctl = "<skill-directory>\scripts\skillctl.py"
python $skillctl doctor
python $skillctl --platform codex status
python $skillctl --platform all search azure
python $skillctl --platform claude disable deploy --dry-run
python $skillctl --platform claude disable deploy
python $skillctl --platform copilot enable my-skill --dry-run
python $skillctl --platform openclaw disable browser-tools --dry-run
python $skillctl --platform hermes --hermes-scope telegram disable shell-tools --dry-run
```

Mutations require an explicit `--platform codex`, `--platform claude`, or
`--platform copilot`, `--platform openclaw`, or `--platform hermes`. Selectors
are exact names, `group:<name>`, `path:<path>`, or `all`; partial names are
intentionally rejected. Hermes defaults to global scope; pass
`--hermes-scope <platform>` for a platform-specific disable.

## Presets

Use a JSON preset file matching `references/presets.md`:

```powershell
python $skillctl --platform codex preset lean --file .\skill-presets.json --dry-run
python $skillctl --platform codex preset lean --file .\skill-presets.json
```

## Safety

- Do not delete, relocate, or rewrite any installed `SKILL.md`.
- Keep changes scoped to native platform controls.
- Codex edits only its marked TOML block.
- Claude Code edits only `skillOverrides` in its settings JSON.
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
