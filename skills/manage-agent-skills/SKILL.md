---
name: manage-agent-skills
description: Audit installed Agent Skills and manage their host-specific visibility across Codex, Claude Code, Copilot CLI, OpenClaw, Hermes, and Antigravity. Use for skill inventory, duplicate or malformed metadata diagnosis, exact enable/disable changes, Claude visibility states, and reusable presets. Does not install skills or measure actual model token usage.
metadata:
  version: "0.2.0"
---

# Manage Agent Skills

Use `scripts/skillctl.py` for on-demand discovery and scoped changes. Resolve
this skill's directory first; use `python` or `python3` available on the host.
PyYAML is required for safe metadata parsing. Never start a watcher.

## Choose the workflow

- **Inventory:** `--platform all status --json` reports configured state.
- **Diagnose:** `--platform all audit --json` reports malformed metadata,
  duplicate identities, configuration layers, and compatibility limitations.
  `audit --strict` returns nonzero for warnings as well as errors. It does not
  decide whether arbitrary skill instructions are factually up to date.
- **Find:** `--platform <host> search <query>` prints exact names, host keys,
  groups and paths. Search is fuzzy; mutations are exact.
- **Change:** resolve the requested scope, preview with `--dry-run`, then apply
  the same command. Existing user authorization is sufficient; do not ask for
  confirmation again solely because a preview completed.
- **Repeat a profile:** read [references/presets.md](references/presets.md).
- **Check host/version compatibility:** read
  [references/platforms.md](references/platforms.md), including its evidence
  dates and limits. Do not infer current host support from a cache directory.

```powershell
$skillctl = "<skill-directory>/scripts/skillctl.py"
python $skillctl --platform all audit --json
python $skillctl --platform codex search deploy
python $skillctl --platform codex disable deploy --dry-run
python $skillctl --platform codex disable deploy
```

Put global options before the command; `--json` also works after it.
Mutations require one explicit platform. Supported selectors are exact name,
`group:<name>`, `path:<native-absolute-path>`, and `all`. Quote selectors with
spaces. Names can match multiple installed copies: review every target.

Unknown names fail by default. Only use `--allow-undiscovered` for an explicitly
requested literal host key on Copilot/OpenClaw/Hermes; it is not typo correction.
Groups normally identify discovery sources. A `--groups file.json` can define
user taxonomy; never infer groups from skill names without that file.

## Host-specific decisions

**Codex:** default writes documented path rules. Existing name rules are
preserved. Use `--codex-selector name` only when the exact host build has been
verified to support name selectors. In that explicit mode, `reconcile`
converts managed plugin paths into stable names; default reconcile preserves
paths. Enabling cannot override a disable outside the manager's block.
Re-scan after a plugin update; path rules may refer to an old version.

**Claude Code:** `enable` / `disable` map to `on` / `off`. Use `set name-only`
to keep discoverability with less description text, or `set user-invocable-only`
for manual invocation. Target directory keys printed by `search`. Only the
selected settings file is written; project, managed, frontmatter and plugin
controls can affect the actual host result.

**Copilot / OpenClaw / Hermes:** use their native configuration semantics.
OpenClaw requires its CLI for writes. Hermes `--hermes-scope telegram` targets
a platform list; global disables still win. Its essential `hermes-agent` skill
cannot be disabled, even with `--force`.

**Antigravity:** select the surface explicitly when managing CLI skills:

```text
<python> <skillctl> --platform antigravity --antigravity-surface cli status
<python> <skillctl> --platform antigravity --antigravity-surface app disable demo --dry-run
```

App/IDE defaults to `~/.gemini/config`; current CLI defaults to
`~/.gemini/antigravity-cli`. `--antigravity-config` overrides either root.
This adapter parks whole global skill directories in sibling `skills_disabled`;
explain that move in the preview. It does not manage workspace/plugin copies.
Do not equate a parked directory with verified absence from a prompt.

## Recovery and reporting

- Keep installed files unchanged, except the documented Antigravity directory
  moves. Never overwrite a parked directory or silently recover a failed move.
- Configuration writes preserve unrelated values. JSONC/YAML formatting can
  normalize. The first backup is `.manage-agent-skills.bak`; later backups have
  unique suffixes. Use the exact returned backup path for recovery.
- Protected authoring/management skills require explicit `--force` to disable.
  Do not add it automatically to make a bulk operation succeed.
- Presets preflight all actions; runtime failures may still leave partial
  changes. Native OpenClaw batches are not transactional. Stop, inspect current
  state and report recovery evidence before retrying.
- Report platform/surface, targets, configured scope, changes, backup/move paths,
  and restart guidance. Distinguish preview, saved configuration, and live host
  verification. Do not claim measured context savings from local state alone.
- `doctor` / `status` continue past a broken adapter and return nonzero. Treat
  zero discovered skills on an errored adapter as unknown, not an empty host.

See [references/auditing.md](references/auditing.md) for compatibility review,
exit codes, recovery, and a safe upgrade validation procedure.
