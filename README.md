<div align="center">

# Manage Agent Skills

### One safe switchboard for Agent Skills across coding agents

Discover, audit, enable, disable, and preset installed skills without deleting
their files — and without running a daemon.

Built by [Han (@yunze7373)](https://github.com/yunze7373) ·
[Yonro Co., Ltd.](https://yonro.co.jp)

[![CI](https://github.com/yunze7373/manage-agent-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/yunze7373/manage-agent-skills/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-111827)](https://agentskills.io/)
[![Hosts](https://img.shields.io/badge/hosts-5-8B5CF6)](#host-platform-support)
[![OS](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-2563EB)](#operating-system-support)
[![No daemon](https://img.shields.io/badge/daemon-none-10B981)](#safety-by-design)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Quick start](#quick-start) ·
[OS support](#operating-system-support) ·
[Hosts](#host-platform-support) ·
[Safety](#safety-by-design) ·
[Presets](#presets) ·
[Contributing](#contributing)

</div>

---

## Why

Agent Skills are increasingly portable: the same `SKILL.md` can work in
multiple agents. Skill enablement is not portable. Every host discovers skills
from different locations and exposes a different visibility mechanism.

Manage Agent Skills provides one CLI and one reusable Agent Skill over a set of
host-specific adapters:

- files stay installed and updateable;
- unused skills can be removed from a host's active catalog;
- newly installed skills appear on the next invocation;
- every mutation can be previewed with `--dry-run`;
- unrelated host configuration is preserved.

> [!IMPORTANT]
> This project is intentionally on-demand. It does not start a background
> service, watch skill directories, or move/delete installed skills.

## Operating system support

The CLI supports Windows, macOS, and Linux with Python 3.11 or newer. Paths are
interpreted using the native rules of the operating system where `skillctl.py`
is running.

| Operating system | Exact-path form | CI |
| --- | --- | :---: |
| Windows | `path:C:\skills\demo\SKILL.md` | ✅ |
| macOS | `path:/Users/alice/.agents/skills/demo/SKILL.md` | ✅ |
| Linux | `path:/home/alice/.agents/skills/demo/SKILL.md` | ✅ |

Use the absolute path reported by `search`. Quote the complete selector when
the path contains spaces. A path from one operating system is not translated
into another operating system's path syntax.

## Host platform support

| Host | Discovery | Native control | Support |
| --- | --- | --- | :---: |
| OpenAI Codex | Shared, Codex, and plugin skill roots | Managed `skills.config` overrides | ✅ |
| Anthropic Claude Code | User and project skill roots | `skillOverrides`, four visibility states | ✅ |
| GitHub Copilot CLI | Native inventory with filesystem fallback | `disabledSkills` | ✅ |
| OpenClaw | Native inventory with workspace/user fallback | `skills.entries.<key>.enabled` through the native CLI | ✅ |
| Hermes Agent | `$HERMES_HOME/skills` | Global or per-platform disable lists | ✅ |
| Claude Desktop / Cowork | Account-synced, no local config | — | ❌ by design |

Claude Desktop and Cowork enable skills per claude.ai account and sync them at
session start, so no local adapter can toggle them. Manage those from
Customize > Skills in the app.

Detailed semantics and primary references live in
[platforms.md](skills/manage-agent-skills/references/platforms.md).

## Quick start

### Windows PowerShell

```powershell
git clone https://github.com/yunze7373/manage-agent-skills.git
Set-Location manage-agent-skills
python -m pip install -r requirements.txt

$skillctl = ".\skills\manage-agent-skills\scripts\skillctl.py"
python $skillctl doctor
python $skillctl --platform all status
python $skillctl --platform all search azure
```

### macOS or Linux

```bash
git clone https://github.com/yunze7373/manage-agent-skills.git
cd manage-agent-skills
python3 -m pip install -r requirements.txt

skillctl="./skills/manage-agent-skills/scripts/skillctl.py"
python3 "$skillctl" doctor
python3 "$skillctl" --platform all status
python3 "$skillctl" --platform all search azure
```

Preview a change, then apply the same command without `--dry-run`:

```powershell
python $skillctl --platform codex disable azure-cost --dry-run
python $skillctl --platform codex disable azure-cost
```

Mutations require one explicit platform. Partial-name mutation is deliberately
rejected; use an exact name, exact group, exact path, or `all`.

## Install as an Agent Skill

The portable skill package is
[`skills/manage-agent-skills`](skills/manage-agent-skills). Link or copy that
directory into a skill root supported by your agent:

| Host | Typical user skill root |
| --- | --- |
| Codex / Copilot CLI / OpenClaw | `~/.agents/skills` |
| Claude Code | `~/.claude/skills` |
| Hermes Agent | `$HERMES_HOME/skills` |

Keeping a symbolic link to the clone makes future updates immediate. You can
also run the Python CLI directly without installing the Agent Skill.

## Commands

```text
doctor
status
search QUERY
enable SELECTOR...
disable SELECTOR...
set STATE SELECTOR...          # Claude Code only
preset NAME --file FILE
```

### JSON output

Pass `--json` before or after any command when a script needs structured
output. Both forms are equivalent:

```text
python <skillctl> --platform all --json status
python <skillctl> --platform all status --json
```

### Selectors

| Selector | Example | Meaning |
| --- | --- | --- |
| Exact name | `openai-docs` | Every discovered item with that host-scoped name or identifier |
| Group | `group:claude-user` | Every skill in one group |
| Exact path | `path:<native-absolute-path>` | One installed skill path |
| All | `all` | Every discovered skill on the selected host |

### Groups

By default a group is only **where a skill was discovered** — `shared`,
`claude-user`, `codex-project`, a plugin name, and so on. No skill is grouped
by its name out of the box, because any such taxonomy is specific to one
person's machine.

To define your own, pass a JSON file with a `groups` section. Patterns are
`fnmatch` style and match the display name, the host identifier, or the
directory name, case-insensitively:

```json
{ "version": 1, "groups": { "azure": ["azure-*", "entra-*"] } }
```

```powershell
python $skillctl --platform codex --groups my-groups.json disable group:azure --dry-run
```

A preset file may carry the same `groups` section, so one file can serve both
`--groups` and `--file`.

Exact-path examples:

```text
Windows: path:C:\skills\demo\SKILL.md
macOS:   path:/Users/alice/.agents/skills/demo/SKILL.md
Linux:   path:/home/alice/.agents/skills/demo/SKILL.md
```

For example, quote a selector containing spaces:

```powershell
python $skillctl --platform codex disable 'path:C:\Agent Skills\demo\SKILL.md' --dry-run
```

```bash
python3 "$skillctl" --platform codex disable 'path:/home/alice/Agent Skills/demo/SKILL.md' --dry-run
```

`manage-agent-skills`, `skill-creator`, and `plugin-creator` are protected from
bulk disable operations unless `--force` is explicitly supplied.

## Common workflows

### Codex

```powershell
python $skillctl --platform codex status
python $skillctl --platform codex disable group:azure --dry-run
```

### Claude Code

```powershell
python $skillctl --platform claude disable legacy-context --dry-run
python $skillctl --platform claude enable legacy-context
```

Claude Code has four visibility states, reachable with `set`:

| State | Listed to Claude | In `/` menu |
| --- | --- | --- |
| `on` | Name and description | Yes |
| `name-only` | Name only | Yes |
| `user-invocable-only` | Hidden | Yes |
| `off` | Hidden | Hidden |

```powershell
# Cut idle context but keep the skill discoverable and invocable
python $skillctl --platform claude set name-only legacy-context

# Keep it manually callable, out of automatic model discovery
python $skillctl --platform claude set user-invocable-only deploy
```

Claude Code keys personal and project skills by **directory name**. The
frontmatter `name` is only a display label, so a skill in
`~/.claude/skills/memory-manager/` with `name: Memory Manager` is keyed
`memory-manager`. `search` prints the identifier as `(key=...)` whenever the
two differ, and mutations report `display_name_differs`.

Two current limits: the adapter reads and writes `~/.claude/settings.json`
only, so a project-level `.claude/settings.local.json` entry can override the
result; and plugin and bundled skills are not discovered — manage those with
`/plugin` and `disableBundledSkills`.

### GitHub Copilot CLI

```powershell
python $skillctl --platform copilot disable unused-skill --dry-run
```

### OpenClaw

```powershell
python $skillctl --platform openclaw disable browser-tools --dry-run
```

OpenClaw mutations are delegated to `openclaw config set`; the manager does not
rewrite its JSON5 configuration. Agent-level allowlists can still narrow the
final visible set.

### Hermes Agent

```powershell
# Global
python $skillctl --platform hermes disable shell-tools --dry-run

# Only on Telegram
python $skillctl --platform hermes --hermes-scope telegram disable shell-tools --dry-run
```

Hermes global disables take precedence over platform-specific state.

## Presets

Presets make repeatable profiles such as `lean`, `coding`, or `research`
portable:

```powershell
python $skillctl --platform codex preset lean --file skill-presets.example.json --dry-run
```

The format supports per-platform `disable` and `enable` selectors. Disable
runs first, so an explicit enable wins. See
[presets.md](skills/manage-agent-skills/references/presets.md).

## How it works

```mermaid
flowchart LR
    U["User or agent"] --> C["skillctl.py"]
    C --> D["Fresh discovery"]
    D --> A{"Host adapter"}
    A --> CX["Codex TOML"]
    A --> CL["Claude JSON"]
    A --> CP["Copilot JSONC"]
    A --> OC["OpenClaw native CLI"]
    A --> HE["Hermes YAML"]
    CX & CL & CP & OC & HE --> R["Status + backup + restart guidance"]
```

The portable interface is shared; discovery and mutation remain host-specific.
That separation prevents the manager from pretending different agents have
identical lifecycle semantics.

## Safety by design

- **Non-destructive:** no installed skill directory or `SKILL.md` is deleted.
- **On-demand:** no daemon, scheduled task, or manager-owned watcher.
- **Previewable:** every mutation supports `--dry-run`.
- **Scoped:** mutations require one explicit platform.
- **Recoverable:** file-backed configuration gets a sibling
  `.manage-agent-skills.bak` before modification.
- **Atomic where owned:** direct config writes use an atomic replacement.
- **Native where safer:** OpenClaw configuration changes use its own CLI.
- **Conservative:** malformed configuration causes a refusal instead of a
  best-effort rewrite.

Review [SECURITY.md](SECURITY.md) before reporting a vulnerability or using
third-party skills.

## Requirements

- Python 3.11 or newer
- PyYAML 6.0 or newer for the Hermes adapter
- The relevant host CLI/configuration for the platform being managed

Codex, Claude Code, Copilot CLI, and OpenClaw fallback discovery use only the
Python standard library.

## Development

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile skills/manage-agent-skills/scripts/skillctl.py
```

Validate the packaged skill when the OpenAI skill-creator utilities are
available:

```text
python <path-to-skill-creator>/scripts/quick_validate.py skills/manage-agent-skills
```

The CI workflow runs the test suite on Linux, Windows, and macOS.

## Contributing

Issues and pull requests are welcome once the repository is published. New
adapters must:

1. document the host's current primary source;
2. implement discovery and `--dry-run` before mutation;
3. preserve unrelated configuration and create recoverable backups;
4. avoid deleting or relocating installed skills;
5. include isolated tests that never touch real user settings.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full checklist.

## Project status

The project is pre-release. The public API, preset schema, and supported host
versions may evolve before `v1.0.0`.

## License

Released under the [MIT License](LICENSE). Copyright © 2026 yunze7373.
