# Platform support

The manager separates the portable Agent Skills format from each host's
enablement mechanism. A directory containing `SKILL.md` is portable; its
visibility controls are not.

## Operating systems

`skillctl.py` supports Windows, macOS, and Linux with Python 3.11 or newer.
Filesystem discovery, home directories, temporary files, atomic replacement,
and exact-path matching use Python's native operating-system path behavior.
The test suite runs on all three operating systems.

An exact-path selector must use the native absolute path reported by `search`:

- Windows: `path:C:\skills\demo\SKILL.md`
- macOS: `path:/Users/alice/.agents/skills/demo/SKILL.md`
- Linux: `path:/home/alice/.agents/skills/demo/SKILL.md`

Quote the complete selector when it contains spaces. The manager does not
translate a Windows path into a POSIX path, or the reverse.

## Host adapters

| Platform | Discovery | Enable/disable backend | Notes |
| --- | --- | --- | --- |
| Codex | `~/.agents/skills`, `$CODEX_HOME/skills`, plugin cache | Managed `[[skills.config]]` block in `config.toml` | Existing rules outside the marked block are preserved and can still win. |
| Claude Code | `~/.claude/skills` plus project/parent `.claude/skills`, one directory deep, stopping at the repository root | `skillOverrides` in JSON settings | Four states: `on`, `name-only`, `user-invocable-only`, `off`. Keyed by directory name. Plugin and bundled skills are out of scope; see below. |
| GitHub Copilot CLI | `copilot skill list --json`, with filesystem fallback | `disabledSkills` in `~/.copilot/settings.json` | JSONC input is accepted; changed files are normalized to JSON after a backup. |
| OpenClaw | Native `openclaw skills list --json`, with workspace/user/shared filesystem fallback | Native `openclaw config set skills.entries["<key>"].enabled` | Honors `metadata.openclaw.skillKey`. Agent-level skill allowlists are a separate, final visibility filter. |
| Hermes Agent | `$HERMES_HOME/skills` (default `~/.hermes/skills`) | `skills.disabled` or `skills.platform_disabled.<scope>` in `config.yaml` | Global disable wins over platform scope. YAML formatting/comments may normalize after a lossless backup. |
| Other hosts | Adapter extension point | Not assumed | Add an adapter only after the host exposes a non-destructive control. |

## Claude Code specifics

Claude Code is the host with the largest gap between "a file exists" and "the
host can see it", so the adapter follows its documented rules exactly:

- **Identity is the directory name.** For a skill under `~/.claude/skills/` or
  `.claude/skills/`, the frontmatter `name` sets only the label shown in
  listings; the command and the `skillOverrides` key both come from the
  directory name. An override written under a differing label silently never
  matches, so the adapter writes the directory name and reports
  `display_name_differs`.
- **Four visibility states**, not two. `name-only` keeps the skill in the `/`
  menu while removing its description from Claude's idle context, which is the
  cheapest way to reduce context without losing discoverability.
- **One directory deep.** Only `<root>/<name>/SKILL.md` is a skill. Files that
  a skill bundles under its own subdirectories are not separate skills.
- **Project roots stop at the repository root.** Without a repository marker
  anywhere above the working directory, the walk continues, so nothing that
  the host might load is dropped from the report.

Known boundaries, not yet covered by an adapter:

- The manager reads and writes `~/.claude/settings.json` only. Claude Code
  resolves `managed > local > project > user`, and its own `/skills` menu
  writes `.claude/settings.local.json`, so a project-level entry can override
  what this manager reports or writes.
- Plugin skills are unaffected by `skillOverrides` and are managed through
  `/plugin`. They are not discovered here.
- Bundled skills such as `/code-review` exist without a file on disk. They can
  be targeted by `skillOverrides`, and switched off in bulk with
  `disableBundledSkills`, but the manager cannot discover them.
- `.claude/commands/*.md`, directory-qualified nested skills such as
  `apps/web:deploy`, and `--add-dir` roots are not discovered.

## Claude Desktop and Cowork

Not supported, and not supportable by a local filesystem adapter. Skills for
the Claude Desktop and Cowork apps are enabled per claude.ai account and synced
at session start; there is no local configuration file that controls
visibility. Skills created inside a Cowork session live in an ephemeral
per-session directory. Manage them from Customize > Skills in the app or from
the skills settings on claude.ai.

The claude.ai account pool, the Skills API pool, and the Claude Code
filesystem pool are separate. A skill uploaded to one is not available in the
others.

## Design rules

- Re-scan on each invocation so newly installed skills appear without a watcher.
- Distinguish installed from enabled.
- Prefer native host controls over moving directories.
- Treat names as host-scoped identifiers. Duplicate names may represent multiple
  installed paths but one host-level visibility key.
- Never claim that extensions, plugins, commands, and Agent Skills have identical
  lifecycle semantics.

The Codex adapter recognizes the legacy `CODEX_SKILL_MANAGER` block created by
`manage-codex-skills`. Its first real mutation rewrites only those markers to
the new format while preserving the entries.

## Adding an adapter

Implement discovery, state reporting, enable, disable, dry-run, and availability
checks. The adapter must preserve unrelated configuration, write atomically when
it owns a config file, and never delete a skill directory.

## Primary references

Verified on 2026-07-29:

- [Agent Skills specification](https://agentskills.io/specification)
- [Codex app-server skill config methods](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code `skillOverrides`](https://code.claude.com/docs/en/settings)
- [GitHub Copilot CLI skill commands](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Copilot CLI `disabledSkills`](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills)
- [OpenClaw configuration CLI](https://docs.openclaw.ai/cli/config)
- [Hermes Agent skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)
- [Hermes Agent configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration/)
- [Hermes native skill configuration implementation](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/skills_config.py)
- [Gemini CLI extensions](https://google-gemini.github.io/gemini-cli/docs/extensions/)
