# Platform support

The manager separates the portable Agent Skills format from each host's
enablement mechanism. A directory containing `SKILL.md` is portable; its
visibility controls are not.

| Platform | Discovery | Enable/disable backend | Notes |
| --- | --- | --- | --- |
| Codex | `~/.agents/skills`, `$CODEX_HOME/skills`, plugin cache | Managed `[[skills.config]]` block in `config.toml` | Existing rules outside the marked block are preserved and can still win. |
| Claude Code | `~/.claude/skills` plus project/parent `.claude/skills` | `skillOverrides` in JSON settings | `off` hides a skill; `on` enables it. Plugin skills use Claude's plugin manager instead. |
| GitHub Copilot CLI | `copilot skill list --json`, with filesystem fallback | `disabledSkills` in `~/.copilot/settings.json` | JSONC input is accepted; changed files are normalized to JSON after a backup. |
| OpenClaw | Native `openclaw skills list --json`, with workspace/user/shared filesystem fallback | Native `openclaw config set skills.entries["<key>"].enabled` | Honors `metadata.openclaw.skillKey`. Agent-level skill allowlists are a separate, final visibility filter. |
| Hermes Agent | `$HERMES_HOME/skills` (default `~/.hermes/skills`) | `skills.disabled` or `skills.platform_disabled.<scope>` in `config.yaml` | Global disable wins over platform scope. YAML formatting/comments may normalize after a lossless backup. |
| Other hosts | Adapter extension point | Not assumed | Add an adapter only after the host exposes a non-destructive control. |

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
