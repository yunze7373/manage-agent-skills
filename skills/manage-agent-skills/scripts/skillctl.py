#!/usr/bin/env python3
"""On-demand, non-destructive Agent Skill manager."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.1"
CODEX_BEGIN = "# BEGIN MANAGE_AGENT_SKILLS:CODEX"
CODEX_END = "# END MANAGE_AGENT_SKILLS:CODEX"
CODEX_LEGACY_BEGIN = "# BEGIN CODEX_SKILL_MANAGER"
CODEX_LEGACY_END = "# END CODEX_SKILL_MANAGER"
PROTECTED_NAMES = {
    "manage-agent-skills",
    "manage-codex-skills",
    "plugin-creator",
    "skill-creator",
}
PLATFORMS = ("codex", "claude", "copilot", "openclaw", "hermes", "antigravity")

# This Antigravity adapter uses directory parking rather than a native per-skill
# switch, so it is the one exception to the
# "never move installed files" rule: a disabled skill is parked, intact, in a
# sibling directory the host does not read, and moved back on enable.
ANTIGRAVITY_ACTIVE_DIR = "skills"
ANTIGRAVITY_DISABLED_DIR = "skills_disabled"

# Claude Code reads personal and project skills one directory deep; plugin and
# vendor trees nest their skills, so those roots keep a recursive scan.
SKILL_GLOB_FLAT = "*/SKILL.md"
SKILL_GLOB_DEEP = "**/SKILL.md"

STATES = ("on", "name-only", "user-only", "off", "unknown")

# Claude Code's four documented skillOverrides values, and the internal state
# label reported for each. "user-only" matches the /skills menu label.
CLAUDE_STATE_FROM_SETTING = {
    "on": "on",
    "name-only": "name-only",
    "user-invocable-only": "user-only",
    "off": "off",
}
CLAUDE_SETTING_FROM_ACTION = {
    "enable": "on",
    "on": "on",
    "name-only": "name-only",
    "user-invocable-only": "user-invocable-only",
    "user-only": "user-invocable-only",
    "disable": "off",
    "off": "off",
}
SET_STATES = ("on", "name-only", "user-invocable-only", "user-only", "off")
BINARY_ACTIONS = {"enable", "disable"}
OPENCLAW_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "OPENCLAW_HOME",
    "OPENCLAW_STATE_DIR",
    "OPENCLAW_WORKSPACE_DIR",
    "OPENCLAW_PROFILE",
    "OPENCLAW_INCLUDE_ROOTS",
)


@dataclass(frozen=True)
class Skill:
    platform: str
    name: str
    path: str
    group: str
    origin: str
    state: str = "unknown"
    config_key: str = ""
    context_visible: bool | None = None

    @property
    def key(self) -> str:
        return path_key(self.path) if self.path else self.name.lower()

    @property
    def identity(self) -> str:
        """The identifier the host's own configuration uses for this skill."""
        return self.config_key or self.name


def path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def read_frontmatter(skill_file: Path) -> dict[str, Any]:
    """Read only a bounded YAML header; never interpret examples in the body."""
    try:
        with skill_file.open(encoding="utf-8-sig") as stream:
            head = stream.read(65536)
    except (OSError, UnicodeError) as exc:
        raise ValueError("unreadable_skill") from exc
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing_frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("unclosed_or_oversized_frontmatter")
    yaml = _load_yaml_module()
    try:
        value = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError("invalid_frontmatter") from exc
    if not isinstance(value, dict):
        raise ValueError("frontmatter_not_mapping")
    return value


def read_skill_name(skill_file: Path) -> str:
    try:
        name = read_frontmatter(skill_file).get("name")
    except ValueError:
        return skill_file.parent.name
    return name.strip() if isinstance(name, str) and name.strip() else skill_file.parent.name


def read_openclaw_skill_key(skill_file: Path, fallback: str) -> str:
    try:
        metadata = read_frontmatter(skill_file).get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        host = metadata.get("openclaw", {}) if isinstance(metadata, dict) else {}
        key = host.get("skillKey") if isinstance(host, dict) else None
    except (ValueError, TypeError):
        return fallback
    return key.strip() if isinstance(key, str) and key.strip() else fallback


def scan_roots(
    platform: str, roots: Iterable[tuple[Path, str, str, str]]
) -> list[Skill]:
    found: dict[str, Skill] = {}
    for root, group, origin, pattern in roots:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob(pattern)):
            relative = skill_file.relative_to(root)
            if platform == "hermes":
                if any(part in {".git", ".hub", ".archive", ".venv", "node_modules", "__pycache__"}
                       for part in relative.parts):
                    continue
                if any(parent.name in {"references", "scripts", "assets", "templates"}
                       and (parent.parent / "SKILL.md").is_file()
                       for parent in skill_file.parents if parent != root):
                    continue
            # Resolve before de-duplicating so one symlinked skill reachable
            # from two roots is counted once, as the hosts themselves do.
            resolved = skill_file.resolve()
            key = path_key(resolved)
            if key in found:
                continue
            # Groups describe where a skill was discovered. Any other taxonomy
            # is machine-specific, so it comes from the user's --groups file.
            name = read_skill_name(skill_file)
            config_key = (
                f"{group}:{name}"
                if platform == "codex" and origin.startswith("plugin:")
                else ""
            )
            found[key] = Skill(
                platform=platform,
                name=name,
                path=str(resolved),
                group=group,
                origin=origin,
                config_key=config_key,
            )
    return sorted(found.values(), key=lambda item: (item.group, item.name, item.key))


def load_group_rules(path: Path | None) -> dict[str, list[str]]:
    """Read an optional user-defined group taxonomy.

    Format: {"version": 1, "groups": {"azure": ["azure-*", "entra-*"]}}
    """
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Group file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid group JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise SystemExit("Group file version must be 1.")
    groups = document.get("groups", {})
    if not isinstance(groups, dict):
        raise SystemExit("Group file groups must be an object.")
    patterns_by_group: dict[str, list[str]] = {}
    for group, patterns in groups.items():
        if not isinstance(patterns, list) or not all(
            isinstance(item, str) for item in patterns
        ):
            raise SystemExit(f"Group {group!r} must map to an array of patterns.")
        patterns_by_group[str(group)] = patterns
    return patterns_by_group


def apply_group_rules(
    inventory: list[Skill], group_patterns: dict[str, list[str]]
) -> list[Skill]:
    """Re-group skills whose name, identifier, or directory matches a pattern."""
    if not group_patterns:
        return inventory
    result: list[Skill] = []
    for item in inventory:
        candidates = {item.name.lower(), item.identity.lower()}
        if item.path:
            candidates.add(Path(item.path).parent.name.lower())
        group = item.group
        for name, patterns in group_patterns.items():
            if any(
                fnmatch(candidate, pattern.lower())
                for candidate in candidates
                for pattern in patterns
            ):
                group = name
                break
        result.append(Skill(**{**asdict(item), "group": group}))
    return sorted(result, key=lambda item: (item.group, item.name, item.key))


def project_skill_roots(
    cwd: Path,
    directory: str,
    platform: str,
    pattern: str = SKILL_GLOB_DEEP,
    stop_at_repo_root: bool = False,
) -> list[tuple[Path, str, str, str]]:
    roots: list[tuple[Path, str, str, str]] = []
    current = cwd.resolve()
    while True:
        root = current / directory / "skills"
        roots.append((root, f"{platform}-project", f"project:{current}", pattern))
        # Claude Code stops at the repository root. Without a repository
        # marker anywhere above, keep walking so nothing is lost.
        if stop_at_repo_root and (current / ".git").exists():
            break
        if current.parent == current:
            break
        current = current.parent
    return roots


def backup_file(path: Path) -> Path:
    """Keep prior recovery copies; never follow an existing backup symlink."""
    backup = path.with_name(path.name + ".manage-agent-skills.bak")
    try:
        handle = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        handle, name = tempfile.mkstemp(prefix=path.name + ".manage-agent-skills.", suffix=".bak", dir=path.parent)
        backup = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream, path.open("rb") as source:
            shutil.copyfileobj(source, stream)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def write_atomic(path: Path, content: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_result: Path | None = None
    if path.exists():
        backup_result = backup_file(path)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return backup_result


def resolve_selectors(selectors: list[str], inventory: list[Skill]) -> list[Skill]:
    selected: dict[str, Skill] = {}
    missing: list[str] = []
    for selector in selectors:
        lowered = selector.lower()
        if lowered == "all":
            matches = inventory
        elif lowered.startswith("group:"):
            wanted = lowered[6:]
            matches = [item for item in inventory if item.group.lower() == wanted]
        elif lowered.startswith("path:"):
            wanted_path = path_key(selector[5:])
            matches = [item for item in inventory if item.path and item.key == wanted_path]
        else:
            # A skill is addressable by its display name or by the identifier
            # its host configuration actually keys on.
            matches = [
                item
                for item in inventory
                if lowered in {item.name.lower(), item.identity.lower()}
            ]
        if not matches:
            missing.append(selector)
        for item in matches:
            selected[item.key] = item
    if missing:
        terms = [term.lower().removeprefix("group:") for term in missing]
        suggestions = sorted(
            {
                item.identity
                for item in inventory
                if any(
                    term in item.name.lower() or term in item.identity.lower()
                    for term in terms
                )
            }
        )[:10]
        suffix = f" Suggestions: {', '.join(suggestions)}" if suggestions else ""
        raise SystemExit(f"No exact skill match: {', '.join(missing)}.{suffix}")
    return list(selected.values())


def require_binary_action(platform: str, action: str) -> None:
    """Only Claude Code exposes the graded skill states."""
    if action not in BINARY_ACTIONS:
        raise SystemExit(
            f"{platform} supports only enable and disable; "
            f"{action!r} is a Claude Code skill state."
        )


class CodexAdapter:
    name = "codex"

    def __init__(self, cwd: Path, config: Path | None = None, selector: str = "path") -> None:
        self.cwd = cwd
        self.selector = selector
        configured_home = os.environ.get("CODEX_HOME")
        self.home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        self.config = config or self.home / "config.toml"

    def available(self) -> tuple[bool, str]:
        return True, str(self.config)

    def _roots(self) -> list[tuple[Path, str, str, str]]:
        roots = [
            *project_skill_roots(self.cwd, ".agents", "codex", SKILL_GLOB_DEEP, True),
            (Path.home() / ".agents" / "skills", "shared", "user:agents", SKILL_GLOB_DEEP),
            (self.home / "skills", "codex", "user:codex", SKILL_GLOB_DEEP),
        ]
        cache = self.home / "plugins" / "cache"
        if not cache.is_dir():
            return roots

        # Inventory installed plugin roots so their host-native configuration
        # state can be audited without changing the installed package files.
        marketplaces = [item for item in cache.iterdir() if item.is_dir()]
        remote_marketplaces = {
            item.name.removesuffix("-remote").lower()
            for item in marketplaces
            if item.name.lower().endswith("-remote")
        }
        for marketplace in marketplaces:
            if not marketplace.is_dir():
                continue
            marketplace_name = marketplace.name.lower()
            logical_marketplace = marketplace_name.removesuffix("-remote")
            # Newer Codex builds materialize remote marketplace bundles under
            # `<marketplace>-remote` while leaving the old hash cache beside it.
            # Only the remote tree is used by the host in that state. Scanning
            # both makes one logical Skill appear twice and can restore stale
            # descriptions that Codex itself no longer references.
            if (
                not marketplace_name.endswith("-remote")
                and marketplace_name in remote_marketplaces
            ):
                continue
            for plugin in marketplace.iterdir():
                if not plugin.is_dir() or plugin.name.startswith("plugin-backup-"):
                    continue
                roots.append(
                    (
                        plugin,
                        plugin.name.lower(),
                        f"plugin:{logical_marketplace}",
                        SKILL_GLOB_DEEP,
                    )
                )
        return roots

    def _load(self) -> tuple[str, dict[str, Any]]:
        if not self.config.exists():
            return "", {}
        text = self.config.read_text(encoding="utf-8")
        try:
            return text, tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"Refusing to edit invalid Codex TOML: {exc}") from exc

    @staticmethod
    def _entries(parsed: dict[str, Any]) -> list[dict[str, Any]]:
        skills = parsed.get("skills", {})
        if not isinstance(skills, dict):
            raise SystemExit("Codex skills must be a table.")
        entries = skills.get("config", [])
        if not isinstance(entries, list):
            raise SystemExit("Codex skills.config must be an array of tables.")
        if any(not isinstance(entry, dict) for entry in entries):
            raise SystemExit("Codex skills.config entries must be tables.")
        return entries

    @staticmethod
    def _managed_span(text: str) -> tuple[int, int, str] | None:
        found: list[tuple[int, int, str]] = []
        for begin, end in (
            (CODEX_BEGIN, CODEX_END),
            (CODEX_LEGACY_BEGIN, CODEX_LEGACY_END),
        ):
            begins = list(re.finditer(r"(?m)^" + re.escape(begin) + r"\r?$", text))
            ends = list(re.finditer(r"(?m)^" + re.escape(end) + r"\r?$", text))
            if not begins and not ends:
                continue
            if len(begins) != 1 or len(ends) != 1 or begins[0].start() >= ends[0].start():
                raise SystemExit("Malformed or duplicate managed Codex markers.")
            start, finish = begins[0].start(), ends[0].end()
            found.append((start, finish, text[start:finish]))
        if len(found) > 1:
            raise SystemExit("Both current and legacy managed Codex blocks exist.")
        return found[0] if found else None

    def _managed_rules(self, text: str) -> set[tuple[str, str]]:
        span = self._managed_span(text)
        if not span:
            return set()
        payload = "\n".join(
            line
            for line in span[2].splitlines()
            if not line.lstrip().startswith("#")
        ).strip()
        if not payload:
            return set()
        parsed = tomllib.loads(payload)
        disabled_entries: set[tuple[str, str]] = set()
        for entry in self._entries(parsed):
            if (entry.get("enabled") is not False
                    or set(entry) - {"name", "path", "enabled"}
                    or sum(isinstance(entry.get(key), str) for key in ("name", "path")) != 1):
                raise SystemExit("Unsupported rule in managed Codex block; preserve and inspect it before editing.")
            if isinstance(entry.get("name"), str):
                disabled_entries.add(("name", entry["name"].lower()))
            elif isinstance(entry.get("path"), str):
                disabled_entries.add(("path", path_key(entry["path"])))
        return disabled_entries

    @staticmethod
    def _disabled_rules(parsed: dict[str, Any]) -> set[tuple[str, str]]:
        disabled_entries: set[tuple[str, str]] = set()
        for entry in CodexAdapter._entries(parsed):
            if entry.get("enabled") is not False:
                continue
            if isinstance(entry.get("name"), str):
                disabled_entries.add(("name", entry["name"].lower()))
            elif isinstance(entry.get("path"), str):
                disabled_entries.add(("path", path_key(entry["path"])))
        return disabled_entries

    @staticmethod
    def _item_rules(item: Skill) -> set[tuple[str, str]]:
        return {
            ("name", item.identity.lower()),
            ("path", item.key),
            ("path", path_key(Path(item.path).parent)) if item.path else ("path", ""),
        }

    @staticmethod
    def _enabled_plugins(parsed: dict[str, Any]) -> set[tuple[str, str]]:
        plugins = parsed.get("plugins", {})
        if not isinstance(plugins, dict):
            return set()
        enabled: set[tuple[str, str]] = set()
        for identifier, settings in plugins.items():
            if not isinstance(identifier, str) or not isinstance(settings, dict):
                continue
            plugin, separator, marketplace = identifier.rpartition("@")
            if separator and plugin and marketplace and settings.get("enabled") is True:
                enabled.add((marketplace.lower(), plugin.lower()))
        return enabled

    @staticmethod
    def _plugin_enabled(item: Skill, enabled: set[tuple[str, str]]) -> bool:
        if not item.origin.startswith("plugin:"):
            return True
        marketplace = item.origin.removeprefix("plugin:").lower()
        return (marketplace, item.group.lower()) in enabled

    def _should_hide(
        self,
        item: Skill,
        disabled: set[tuple[str, str]],
        enabled_plugins: set[tuple[str, str]],
    ) -> bool:
        return bool(self._item_rules(item) & disabled) or not self._plugin_enabled(
            item, enabled_plugins
        )

    @staticmethod
    def _plugin_identity_from_path(value: str) -> str | None:
        path = Path(value)
        parts = path.parts
        lowered = [part.lower() for part in parts]
        cache_index = next(
            (
                index
                for index in range(len(lowered) - 1)
                if lowered[index] == "plugins" and lowered[index + 1] == "cache"
            ),
            None,
        )
        if cache_index is None or cache_index + 3 >= len(parts):
            return None
        plugin = parts[cache_index + 3]
        skills_index = next(
            (
                index
                for index in range(cache_index + 4, len(lowered))
                if lowered[index] == "skills"
            ),
            None,
        )
        if skills_index is None or skills_index + 1 >= len(parts):
            return None
        name = read_skill_name(path) if path.exists() else parts[-2]
        return f"{plugin}:{name}".lower()

    @staticmethod
    def _normalize_managed_rules(
        managed: set[tuple[str, str]], inventory: list[Skill]
    ) -> set[tuple[str, str]]:
        """Replace version-bound plugin paths with stable host identities."""
        plugin_by_path = {
            item.key: item
            for item in inventory
            if item.origin.startswith("plugin:")
        }
        normalized: set[tuple[str, str]] = set()
        for kind, value in managed:
            item = plugin_by_path.get(value) if kind == "path" else None
            if item is not None:
                normalized.add(("name", item.identity.lower()))
            elif kind == "path" and (
                identity := CodexAdapter._plugin_identity_from_path(value)
            ):
                normalized.add(("name", identity))
            else:
                normalized.add((kind, value))
        return normalized

    def discover(self) -> list[Skill]:
        inventory = scan_roots(self.name, self._roots())
        text, parsed = self._load()
        del text
        disabled = self._disabled_rules(parsed)
        enabled_plugins = self._enabled_plugins(parsed)
        inventory = [
            Skill(
                **{
                    **asdict(item),
                    "state": "off"
                    if self._should_hide(item, disabled, enabled_plugins)
                    else "on",
                    # Codex's current local config controls enablement, but it
                    # does not expose a supported per-Skill proof that a host
                    # omitted metadata from a model prompt.
                    "context_visible": None,
                }
            )
            for item in inventory
        ]
        return sorted(inventory, key=lambda item: (item.group, item.name, item.key))

    def _render(
        self, rules: set[tuple[str, str]], inventory: list[Skill]
    ) -> str:
        by_key = {item.key: item.path for item in inventory}
        by_name = {item.identity.lower(): item.identity for item in inventory}
        lines = [
            CODEX_BEGIN,
            "# Managed by manage-agent-skills. Installed Skill files are never modified.",
            "",
        ]
        for kind, key in sorted(rules):
            value = by_name.get(key, key) if kind == "name" else by_key.get(key, key)
            encoded = json.dumps(value, ensure_ascii=False) if any(c in value for c in "'\r\n") else f"'{value}'"
            selector = "name" if kind == "name" else "path"
            lines.extend(
                ["[[skills.config]]", f"{selector} = {encoded}", "enabled = false", ""]
            )
        lines.append(CODEX_END)
        return "\n".join(lines)

    def _replace(
        self,
        text: str,
        rules: set[tuple[str, str]],
        inventory: list[Skill],
    ) -> str:
        span = self._managed_span(text)
        if not span and not rules:
            return text
        block = self._render(rules, inventory)
        if span:
            updated = text[: span[0]] + block + text[span[1] :]
        else:
            separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
            updated = text + separator + block + "\n"
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"Generated invalid Codex TOML: {exc}") from exc
        return updated

    def reconcile(self, dry_run: bool) -> dict[str, Any]:
        text, parsed = self._load()
        managed = self._managed_rules(text)
        inventory = self.discover()
        normalized = (
            self._normalize_managed_rules(managed, inventory)
            if self.selector == "name" else managed
        )
        migrated_rules = sum(
            self.selector == "name" and kind == "path" and self._plugin_identity_from_path(value) is not None
            for kind, value in managed
        )
        updated = self._replace(text, normalized, inventory)
        changed = updated != text
        backup = None
        if changed and not dry_run:
            backup = write_atomic(self.config, updated)
            self._load()
        return {
            "platform": self.name,
            "action": "reconcile",
            "changed": changed,
            "dry_run": dry_run,
            "rules_migrated": migrated_rules,
            "backup": str(backup) if backup else None,
            "context_visibility": "unverified",
            "note": "Codex does not provide a supported local proof that disabled metadata is absent from the model prompt; installed Skill files were not changed.",
            "new_session_recommended": changed and not dry_run,
        }

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        require_binary_action(self.name, action)
        text, parsed = self._load()
        managed = self._managed_rules(text)
        span = self._managed_span(text)
        outside = text[:span[0]] + text[span[1]:] if span else text
        external = self._disabled_rules(tomllib.loads(outside))
        target_rules = set().union(*(self._item_rules(item) for item in targets))
        target_name_rules = {
            ("name", item.identity.lower()) if self.selector == "name"
            else ("path", item.key) for item in targets
            if not self._item_rules(item) & external
        }
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
            desired = managed | {
                rule for rule in target_name_rules if rule not in external
            }
        else:
            blocked = sorted(
                {
                    item.name
                    for item in targets
                    if self._item_rules(item) & external
                }
            )
            if blocked:
                raise SystemExit(
                    "Skills disabled outside this manager's block were preserved: "
                    + ", ".join(blocked)
                )
            desired = managed - target_rules
        updated = self._replace(text, desired, [*self.discover(), *targets])
        changed = updated != text
        backup = None
        if changed and not dry_run:
            backup = write_atomic(self.config, updated)
            self._load()
        return {
            "platform": self.name,
            "action": action,
            "targets": sorted({item.name for item in targets}),
            "changed": changed,
            "dry_run": dry_run,
            "config": str(self.config),
            "backup": str(backup) if backup else None,
            "context_visibility": "unverified",
            "selector_mode": self.selector,
            "note": "Only Codex's documented configuration was changed; installed Skill files were not modified.",
            "new_session_recommended": changed and not dry_run,
        }


class ClaudeAdapter:
    name = "claude"

    def __init__(self, cwd: Path, config: Path | None = None) -> None:
        self.cwd = cwd
        self.config = config or Path.home() / ".claude" / "settings.json"

    def available(self) -> tuple[bool, str]:
        return True, str(self.config)

    def _roots(self) -> list[tuple[Path, str, str, str]]:
        return [
            (
                Path.home() / ".claude" / "skills",
                "claude-user",
                "user:claude",
                SKILL_GLOB_FLAT,
            ),
            *project_skill_roots(
                self.cwd, ".claude", "claude", SKILL_GLOB_FLAT, True
            ),
        ]

    def _load(self) -> dict[str, Any]:
        if not self.config.exists():
            return {}
        try:
            value = json.loads(self.config.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Refusing to edit invalid Claude settings JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise SystemExit("Claude settings JSON must contain an object.")
        overrides = value.get("skillOverrides", {})
        if not isinstance(overrides, dict):
            raise SystemExit("Claude skillOverrides must be an object.")
        return value

    @staticmethod
    def command_name(skill_file: Path) -> str:
        """Claude Code keys personal and project skills by directory name.

        Frontmatter ``name`` only sets the label shown in skill listings, so an
        override written under that label would never match.
        """
        return skill_file.parent.name

    def discover(self) -> list[Skill]:
        settings = self._load()
        overrides = settings.get("skillOverrides", {})
        result: list[Skill] = []
        for item in scan_roots(self.name, self._roots()):
            command = self.command_name(Path(item.path))
            configured = overrides.get(command)
            state = (
                "on"
                if configured is None
                else CLAUDE_STATE_FROM_SETTING.get(str(configured), "unknown")
            )
            result.append(
                Skill(**{**asdict(item), "state": state, "config_key": command})
            )
        return result

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        value = CLAUDE_SETTING_FROM_ACTION.get(action)
        if value is None:
            raise SystemExit(
                f"Unsupported Claude skill state: {action!r}. "
                "Use one of: " + ", ".join(SET_STATES)
            )
        if value == "off":
            protected = sorted(
                {
                    item.identity
                    for item in targets
                    if PROTECTED_NAMES & {item.identity, item.name}
                }
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        settings = self._load()
        overrides = dict(settings.get("skillOverrides", {}))
        before = dict(overrides)
        labels = {
            item.name: item.identity
            for item in targets
            if item.path and item.name != item.identity
        }
        for item in targets:
            overrides[item.identity] = value
        settings["skillOverrides"] = dict(sorted(overrides.items()))
        changed = overrides != before
        backup = None
        if changed and not dry_run:
            content = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
            backup = write_atomic(self.config, content)
            self._load()
        result = {
            "platform": self.name,
            "action": action,
            "state": value,
            "targets": sorted({item.identity for item in targets}),
            "changed": changed,
            "dry_run": dry_run,
            "config": str(self.config),
            "backup": str(backup) if backup else None,
            "new_session_recommended": changed and not dry_run,
        }
        if labels:
            result["display_name_differs"] = labels
        return result


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def parse_copilot_inventory(payload: Any) -> list[Skill]:
    found: dict[str, Skill] = {}
    for item in _walk_json(payload):
        name = item.get("name")
        kind = str(item.get("kind", item.get("type", ""))).lower()
        path = item.get("path", item.get("location", ""))
        if not isinstance(path, str):
            continue
        if path and Path(path).is_dir():
            path = str(Path(path) / "SKILL.md")
        if not isinstance(name, str):
            continue
        if kind and "skill" not in kind:
            continue
        if not kind and "enabled" not in item and not path:
            continue
        enabled = item.get("enabled")
        state = "on" if enabled is True else "off" if enabled is False else "unknown"
        key = path_key(path) if path else name.lower()
        found[key] = Skill(
            platform="copilot",
            name=name,
            path=str(path),
            group=str(item.get("scope", "copilot")),
            origin="copilot-cli",
            state=state,
        )
    return sorted(found.values(), key=lambda skill: (skill.group, skill.name, skill.key))


def parse_jsonc(text: str) -> Any:
    """Parse the JSONC subset used by agent settings files."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            index += 2
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return json.loads("".join(output))


class CopilotAdapter:
    name = "copilot"

    def __init__(self, cwd: Path, config: Path | None = None) -> None:
        self.cwd = cwd
        self.executable = shutil.which("copilot")
        configured_home = os.environ.get("COPILOT_HOME")
        self.home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".copilot"
        )
        self.config = config or self.home / "settings.json"

    def available(self) -> tuple[bool, str]:
        detail = str(self.config)
        if not self.executable:
            detail += " (filesystem discovery; copilot executable not found)"
        return True, detail

    def _load(self) -> dict[str, Any]:
        if not self.config.exists():
            return {}
        try:
            value = parse_jsonc(self.config.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Refusing to edit invalid Copilot settings JSONC: {exc}") from exc
        if not isinstance(value, dict):
            raise SystemExit("Copilot settings JSON must contain an object.")
        disabled = value.get("disabledSkills", [])
        if not isinstance(disabled, list) or not all(
            isinstance(item, str) for item in disabled
        ):
            raise SystemExit("Copilot disabledSkills must be an array of strings.")
        return value

    def _fallback(self) -> list[Skill]:
        roots = [
            (
                self.home / "skills",
                "copilot-user",
                "user:copilot",
                SKILL_GLOB_DEEP,
            ),
            (Path.home() / ".agents" / "skills", "shared", "user:agents", SKILL_GLOB_DEEP),
            *project_skill_roots(self.cwd, ".github", "copilot"),
            *project_skill_roots(self.cwd, ".agents", "shared"),
            *project_skill_roots(self.cwd, ".claude", "claude-compatible"),
        ]
        return scan_roots(self.name, roots)

    def discover(self) -> list[Skill]:
        disabled = {
            name.lower() for name in self._load().get("disabledSkills", [])
        }
        if not self.executable:
            inventory = self._fallback()
        else:
            result = subprocess.run(
                [self.executable, "skill", "list", "--json"],
                cwd=self.cwd,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            if result.returncode != 0:
                inventory = self._fallback()
            else:
                try:
                    parsed = parse_copilot_inventory(json.loads(result.stdout))
                except json.JSONDecodeError:
                    parsed = []
                inventory = parsed or self._fallback()
        return [
            Skill(
                **{
                    **asdict(item),
                    "state": "off" if item.name.lower() in disabled or item.state == "off" else "on",
                }
            )
            for item in inventory
        ]

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        require_binary_action(self.name, action)
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        settings = self._load()
        disabled = {
            name.lower(): name for name in settings.get("disabledSkills", [])
        }
        before = dict(disabled)
        for item in targets:
            if action == "disable":
                disabled[item.name.lower()] = item.name
            else:
                disabled.pop(item.name.lower(), None)
        settings["disabledSkills"] = sorted(
            disabled.values(), key=lambda value: value.lower()
        )
        changed = disabled != before
        backup = None
        if changed and not dry_run:
            content = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
            backup = write_atomic(self.config, content)
            self._load()
        return {
            "platform": self.name,
            "action": action,
            "targets": sorted({item.name for item in targets}),
            "changed": changed,
            "dry_run": dry_run,
            "config": str(self.config),
            "backup": str(backup) if backup else None,
            "new_session_recommended": changed and not dry_run,
        }


def parse_openclaw_inventory(payload: Any) -> list[Skill]:
    found: dict[str, Skill] = {}
    for item in _walk_json(payload):
        name = item.get("name", item.get("skillName"))
        kind = str(item.get("kind", item.get("type", ""))).lower()
        path = item.get(
            "path",
            item.get("location", item.get("skillFile", item.get("skillDir", ""))),
        )
        if not isinstance(name, str):
            continue
        if kind and "skill" not in kind:
            continue
        if not kind and not any(
            key in item
            for key in ("enabled", "disabled", "eligible", "path", "location", "skillKey")
        ):
            continue
        enabled = item.get("enabled")
        disabled = item.get("disabled")
        state = (
            "off"
            if disabled is True or enabled is False
            else "on"
            if enabled is True
            else "unknown"
        )
        key = str(item.get("skillKey", item.get("key", name)))
        identity = path_key(path) if path else name.lower()
        found[identity] = Skill(
            platform="openclaw",
            name=name,
            path=str(path),
            group=str(item.get("source", item.get("scope", "openclaw"))),
            origin="openclaw-cli",
            state=state,
            config_key=key,
        )
    return sorted(found.values(), key=lambda skill: (skill.group, skill.name, skill.key))


class OpenClawAdapter:
    name = "openclaw"

    def __init__(self, cwd: Path, config: Path | None = None) -> None:
        self.cwd = cwd
        self.executable = shutil.which("openclaw")
        configured_state = os.environ.get("OPENCLAW_STATE_DIR")
        self.home = (
            Path(configured_state).expanduser()
            if configured_state
            else Path.home() / ".openclaw"
        )
        configured_path = os.environ.get("OPENCLAW_CONFIG_PATH")
        self.config = (
            config
            or (Path(configured_path).expanduser() if configured_path else None)
            or self.home / "openclaw.json"
        )

    def available(self) -> tuple[bool, str]:
        detail = str(self.config)
        if not self.executable:
            detail += " (read-only filesystem fallback; openclaw executable not found)"
        return self.executable is not None, detail

    def _environment(self) -> dict[str, str]:
        # The native CLI only needs host paths and its documented path/profile
        # selectors. Do not pass unrelated provider/API credentials to it.
        environment = {
            key: os.environ[key]
            for key in OPENCLAW_CHILD_ENV_KEYS
            if key in os.environ
        }
        environment["OPENCLAW_CONFIG_PATH"] = str(self.config)
        return environment

    def _native_json(self, *arguments: str) -> Any | None:
        if not self.executable:
            return None
        result = subprocess.run(
            [self.executable, *arguments],
            cwd=self.cwd,
            env=self._environment(),
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def _skill_config(self) -> dict[str, Any]:
        native = self._native_json("config", "get", "skills", "--json")
        if isinstance(native, dict):
            value = native.get("skills", native)
            return value if isinstance(value, dict) else {}
        if not self.config.exists():
            return {}
        try:
            document = parse_jsonc(self.config.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(document, dict):
            return {}
        value = document.get("skills", {})
        return value if isinstance(value, dict) else {}

    def _roots(self, skills_config: dict[str, Any]) -> list[tuple[Path, str, str, str]]:
        roots = [
            (
                self.cwd / "skills",
                "openclaw-workspace",
                f"workspace:{self.cwd}",
                SKILL_GLOB_DEEP,
            ),
            (
                self.cwd / ".agents" / "skills",
                "shared-project",
                f"workspace:{self.cwd}",
                SKILL_GLOB_DEEP,
            ),
            (Path.home() / ".agents" / "skills", "shared", "user:agents", SKILL_GLOB_DEEP),
            (self.home / "skills", "openclaw-user", "user:openclaw", SKILL_GLOB_DEEP),
            (
                self.home / "workspace" / "skills",
                "openclaw-workspace",
                "default-workspace",
                SKILL_GLOB_DEEP,
            ),
            (
                self.home / "workspace" / ".agents" / "skills",
                "shared-project",
                "default-workspace",
                SKILL_GLOB_DEEP,
            ),
        ]
        load = skills_config.get("load", {})
        extra_dirs = load.get("extraDirs", []) if isinstance(load, dict) else []
        if isinstance(extra_dirs, list):
            for value in extra_dirs:
                if isinstance(value, str):
                    roots.append(
                        (
                            Path(value).expanduser(),
                            "openclaw-extra",
                            "skills.load.extraDirs",
                            SKILL_GLOB_DEEP,
                        )
                    )
        return roots

    @staticmethod
    def _entries(skills_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        value = skills_config.get("entries", {})
        if not isinstance(value, dict):
            return {}
        return {
            str(key): entry
            for key, entry in value.items()
            if isinstance(entry, dict)
        }

    def discover(self) -> list[Skill]:
        skills_config = self._skill_config()
        entries = self._entries(skills_config)
        native = self._native_json("skills", "list", "--json")
        inventory = parse_openclaw_inventory(native) if native is not None else []
        if not inventory:
            inventory = scan_roots(self.name, self._roots(skills_config))
            inventory = [
                Skill(
                    **{
                        **asdict(item),
                        "config_key": read_openclaw_skill_key(
                            Path(item.path), item.name
                        ),
                    }
                )
                for item in inventory
            ]
        lowered_entries = {key.lower(): value for key, value in entries.items()}
        result: list[Skill] = []
        for item in inventory:
            config_key = item.config_key or item.name
            override = entries.get(config_key, lowered_entries.get(config_key.lower(), {}))
            state = (
                "off"
                if override.get("enabled") is False
                else "on"
                if override.get("enabled") is True or item.state == "on"
                else "unknown"
            )
            result.append(
                Skill(**{**asdict(item), "state": state, "config_key": config_key})
            )
        return result

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        require_binary_action(self.name, action)
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        value = "false" if action == "disable" else "true"
        executable = self.executable or "openclaw"
        commands = []
        for item in targets:
            key = item.config_key or item.name
            path = f"skills.entries[{json.dumps(key, ensure_ascii=False)}].enabled"
            commands.append([executable, "config", "set", path, value])
        changed = any(
            item.state != ("off" if action == "disable" else "on")
            for item in targets
        )
        backup = None
        if changed and not dry_run:
            if not self.executable:
                raise SystemExit(
                    "OpenClaw mutations require the openclaw executable; "
                    "use --dry-run to preview the native commands."
                )
            if self.config.exists():
                backup = backup_file(self.config)
            completed = 0
            for command in commands:
                try:
                    result = subprocess.run(
                        command,
                        cwd=self.cwd,
                        env=self._environment(),
                        shell=False,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=20,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise SystemExit(
                        f"OpenClaw execution interrupted after {completed} commands. "
                        f"Current command outcome is unknown; inspect host state. Backup: {backup}"
                    ) from exc
                if result.returncode != 0:
                    raise SystemExit(
                        f"OpenClaw config command failed ({result.returncode}) after {completed} commands. "
                        f"Partial changes may remain; inspect host state. Backup: {backup}"
                    )
                completed += 1
        return {
            "platform": self.name,
            "action": action,
            "targets": sorted({item.name for item in targets}),
            "changed": changed,
            "dry_run": dry_run,
            "config": str(self.config),
            "backup": str(backup) if backup else None,
            "commands": commands,
            "new_session_recommended": changed and not dry_run,
            "note": "Agent skill allowlists can further restrict visibility.",
        }


def _load_yaml_module() -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "Skill metadata and Hermes support require PyYAML. Install requirements.txt first."
        ) from exc
    return yaml


def _normalize_string_values(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise SystemExit(f"{label} must be a string, null, or an array of strings.")


class HermesAdapter:
    name = "hermes"

    def __init__(
        self, cwd: Path, config: Path | None = None, scope: str = "global"
    ) -> None:
        del cwd
        configured_home = os.environ.get("HERMES_HOME")
        default_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".hermes"
        )
        self.config = config or default_home / "config.yaml"
        self.home = self.config.parent if config else default_home
        self.scope = scope
        self.executable = shutil.which("hermes")

    def available(self) -> tuple[bool, str]:
        try:
            _load_yaml_module()
            yaml_available = True
        except SystemExit:
            yaml_available = False
        available = yaml_available and (
            self.executable is not None or self.home.exists()
        )
        detail = f"{self.config} (scope={self.scope})"
        if not self.executable:
            detail += " (hermes executable not found)"
        if not yaml_available:
            detail += " (PyYAML not installed)"
        return available, detail

    def _load(self) -> dict[str, Any]:
        if not self.config.exists():
            return {}
        yaml = _load_yaml_module()
        try:
            value = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SystemExit(f"Refusing to edit invalid Hermes YAML: {exc}") from exc
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise SystemExit("Hermes config.yaml must contain an object.")
        skills = value.get("skills") or {}
        if not isinstance(skills, dict):
            raise SystemExit("Hermes skills configuration must be an object.")
        _normalize_string_values(skills.get("disabled"), "Hermes skills.disabled")
        platform_disabled = skills.get("platform_disabled") or {}
        if not isinstance(platform_disabled, dict):
            raise SystemExit(
                "Hermes skills.platform_disabled must map platform names to values."
            )
        for platform, items in platform_disabled.items():
            _normalize_string_values(
                items, f"Hermes skills.platform_disabled.{platform}"
            )
        return value

    @staticmethod
    def _disabled(
        settings: dict[str, Any], scope: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        skills = settings.get("skills") or {}
        global_values = (
            _normalize_string_values(
                skills.get("disabled"), "Hermes skills.disabled"
            )
            if isinstance(skills, dict)
            else []
        )
        platform_values = (
            _normalize_string_values(
                (skills.get("platform_disabled") or {}).get(scope),
                f"Hermes skills.platform_disabled.{scope}",
            )
            if isinstance(skills, dict) and scope != "global"
            else []
        )
        return (
            {name.lower(): name for name in global_values},
            {name.lower(): name for name in platform_values},
        )

    def discover(self) -> list[Skill]:
        settings = self._load()
        global_disabled, platform_disabled = self._disabled(settings, self.scope)
        global_disabled.pop("hermes-agent", None)
        platform_disabled.pop("hermes-agent", None)
        inventory = scan_roots(
            self.name,
            [
                (
                    self.home / "skills",
                    "hermes-home",
                    f"home:{self.home}",
                    SKILL_GLOB_DEEP,
                )
            ],
        )
        return [
            Skill(
                **{
                    **asdict(item),
                    "state": "off"
                    if item.name.lower() in global_disabled
                    or item.name.lower() in platform_disabled
                    else "on",
                }
            )
            for item in inventory
        ]

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        require_binary_action(self.name, action)
        if action == "disable" and any(item.identity == "hermes-agent" for item in targets):
            raise SystemExit("Hermes essential skill hermes-agent cannot be disabled, including with --force.")
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        settings = self._load()
        if not isinstance(settings.get("skills"), dict):
            settings["skills"] = {}
        skills = settings["skills"]
        global_disabled, platform_disabled = self._disabled(settings, self.scope)
        if self.scope != "global" and action == "enable":
            blocked = sorted(
                item.name
                for item in targets
                if item.name.lower() in global_disabled
            )
            if blocked:
                raise SystemExit(
                    "Globally disabled Hermes skills cannot be enabled only for "
                    f"{self.scope}: " + ", ".join(blocked)
                )
        current = global_disabled if self.scope == "global" else platform_disabled
        before = dict(current)
        for item in targets:
            if action == "disable":
                current[item.name.lower()] = item.name
            else:
                current.pop(item.name.lower(), None)
        values = sorted(current.values(), key=str.lower)
        if self.scope == "global":
            skills["disabled"] = values
        else:
            if not isinstance(skills.get("platform_disabled"), dict):
                skills["platform_disabled"] = {}
            platform_map = skills["platform_disabled"]
            platform_map[self.scope] = values
        changed = current != before
        backup = None
        if changed and not dry_run:
            yaml = _load_yaml_module()
            content = yaml.safe_dump(
                settings,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
            backup = write_atomic(self.config, content)
            self._load()
        return {
            "platform": self.name,
            "action": action,
            "scope": self.scope,
            "targets": sorted({item.name for item in targets}),
            "changed": changed,
            "dry_run": dry_run,
            "config": str(self.config),
            "backup": str(backup) if backup else None,
            "new_session_recommended": changed and not dry_run,
            "note": "Global disables take precedence over platform-scoped disables.",
        }


class AntigravityAdapter:
    """Antigravity / agy CLI: toggle by parking skill directories.

    Active global skills live in ``<config>/skills/<dir>/SKILL.md``.
    Disabled skills are moved unchanged to
    ``<config>/skills_disabled/<dir>/``. Only direct children of those two
    roots are ever moved, a move never overwrites an existing directory, and
    file contents are never touched.
    """

    name = "antigravity"

    def __init__(self, cwd: Path, config: Path | None = None, surface: str = "app") -> None:
        self.cwd = cwd
        self.surface = surface
        configured = os.environ.get("ANTIGRAVITY_CONFIG_HOME")
        self.config = (
            config
            or (Path(configured) if configured else None)
            or Path.home() / ".gemini" / ("antigravity-cli" if surface == "cli" else "config")
        ).expanduser().absolute()
        self.active_root = self.config / ANTIGRAVITY_ACTIVE_DIR
        self.disabled_root = self.config / ANTIGRAVITY_DISABLED_DIR

    def available(self) -> tuple[bool, str]:
        return self.active_root.is_dir() or self.disabled_root.is_dir(), str(
            self.active_root
        )

    def _entries(self, root: Path, state: str) -> list[Skill]:
        result: list[Skill] = []
        if not root.is_dir():
            return result
        for directory in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            skill_file = directory / "SKILL.md"
            if not directory.is_dir() or not skill_file.is_file():
                continue
            # Keep the unresolved entry path: a symlinked skill is parked by
            # moving the link itself, never the tree it points at.
            result.append(
                Skill(
                    platform=self.name,
                    name=read_skill_name(skill_file),
                    path=str(skill_file),
                    group="antigravity-user",
                    origin=f"user:antigravity:{state}",
                    state=state,
                    config_key=directory.name,
                    # Workspace/plugin copies and active sessions are not
                    # observable from this global directory alone.
                    context_visible=None,
                )
            )
        return result

    def discover(self) -> list[Skill]:
        active = self._entries(self.active_root, "on")
        active_keys = {item.config_key.lower() for item in active}
        parked = [
            item
            for item in self._entries(self.disabled_root, "off")
            if item.config_key.lower() not in active_keys
        ]
        return sorted(active + parked, key=lambda item: (item.name, item.key))

    def _managed_directory(self, item: Skill) -> Path:
        directory = Path(item.path).parent
        for root in (self.active_root, self.disabled_root):
            if root.resolve().parent != self.config.resolve():
                raise SystemExit(f"Refusing redirected skill root: {root}")
        if path_key(directory.parent) not in {
            path_key(self.active_root),
            path_key(self.disabled_root),
        }:
            raise SystemExit(
                f"Refusing to move {directory}: not a direct child of "
                f"{self.active_root} or {self.disabled_root}."
            )
        return directory

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        require_binary_action(self.name, action)
        if action == "disable":
            protected = sorted(
                {
                    item.identity
                    for item in targets
                    if PROTECTED_NAMES & {item.identity, item.name}
                }
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        source_root, destination_root, wanted_state = (
            (self.active_root, self.disabled_root, "on")
            if action == "disable"
            else (self.disabled_root, self.active_root, "off")
        )
        moves: list[dict[str, str]] = []
        conflicts: list[dict[str, str]] = []
        unchanged: list[str] = []
        for item in sorted(targets, key=lambda value: value.identity.lower()):
            if not item.path or item.state != wanted_state:
                unchanged.append(item.identity)
                continue
            directory = self._managed_directory(item)
            if path_key(directory.parent) != path_key(source_root):
                unchanged.append(item.identity)
                continue
            destination = destination_root / directory.name
            if destination.exists() or destination.is_symlink():
                conflicts.append({"skill": item.identity, "existing": str(destination)})
                continue
            moves.append({"skill": item.identity, "from": str(directory), "to": str(destination)})
        if conflicts:
            raise SystemExit(
                "Destination skill directories already exist; refusing to move targets: "
                + ", ".join(f"{row['skill']} -> {row['existing']}" for row in conflicts)
            )
        if moves and not dry_run:
            destination_root.mkdir(parents=True, exist_ok=True)
            done: list[dict[str, str]] = []
            try:
                for move in moves:
                    os.rename(move["from"], move["to"])
                    done.append(move)
            except OSError as exc:
                # Put back what already moved so a failure never leaves the
                # host with half of a requested change.
                rollback_failed = []
                for move in reversed(done):
                    try:
                        if Path(move["from"]).exists() or Path(move["from"]).is_symlink():
                            raise OSError("Original location is occupied")
                        os.rename(move["to"], move["from"])
                    except OSError:
                        rollback_failed.append(move)
                if rollback_failed:
                    raise SystemExit(
                        "Move failed; rollback incomplete. Recover these paths manually: "
                        + json.dumps(rollback_failed) + f". Cause: {exc}"
                    ) from exc
                raise SystemExit(
                    f"Move failed and was rolled back: {exc}"
                ) from exc
        return {
            "platform": self.name,
            "action": action,
            "targets": sorted({item.identity for item in targets}),
            "changed": bool(moves),
            "dry_run": dry_run,
            "moves": moves,
            "unchanged": sorted(unchanged),
            "active_root": str(self.active_root),
            "disabled_root": str(self.disabled_root),
            "surface": self.surface,
            "context_visibility": "unverified",
            "new_session_recommended": bool(moves) and not dry_run,
        }


def adapters(args: argparse.Namespace) -> list[Any]:
    selected = PLATFORMS if args.platform == "all" else (args.platform,)
    result: list[Any] = []
    for name in selected:
        if name == "codex":
            result.append(CodexAdapter(args.cwd, args.codex_config, args.codex_selector))
        elif name == "claude":
            result.append(ClaudeAdapter(args.cwd, args.claude_config))
        elif name == "copilot":
            result.append(CopilotAdapter(args.cwd, args.copilot_config))
        elif name == "openclaw":
            result.append(OpenClawAdapter(args.cwd, args.openclaw_config))
        elif name == "antigravity":
            result.append(AntigravityAdapter(args.cwd, args.antigravity_config, args.antigravity_surface))
        else:
            result.append(
                HermesAdapter(args.cwd, args.hermes_config, args.hermes_scope)
            )
    return result


def status_payload(
    selected: list[Any], rules: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    platforms: list[dict[str, Any]] = []
    for adapter in selected:
        available, detail = adapter.available()
        error = None
        try:
            inventory = apply_group_rules(adapter.discover(), rules or {})
        except (SystemExit, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            # Do not serialize parser exceptions: YAML diagnostics can contain secrets.
            error = f"{type(exc).__name__}: configuration or discovery failed; inspect this platform locally"
            inventory = []
        counts = {
            state: sum(item.state == state for item in inventory)
            for state in STATES
        }
        context_counts = {
            "context_visible": sum(item.context_visible is True for item in inventory),
            "context_hidden": sum(item.context_visible is False for item in inventory),
            "context_unverified": sum(item.context_visible is None for item in inventory),
            "context_drift": sum(
                item.state == "off" and item.context_visible is True
                for item in inventory
            ),
        }
        platforms.append(
            {
                "platform": adapter.name,
                "available": available,
                "error": error,
                "state_scope": "adapter configuration; not a live host session",
                "detail": detail,
                "discovered": len(inventory),
                **counts,
                **context_counts,
            }
        )
    return {"platforms": platforms}


def audit_payload(
    selected: list[Any], rules: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Configuration/metadata evidence only; no host prompt or token-cost claims."""
    findings: list[dict[str, Any]] = []
    inventories: list[dict[str, Any]] = []

    def add(platform: str, severity: str, code: str, detail: str, path: str = "") -> None:
        findings.append(dict(platform=platform, severity=severity, code=code, detail=detail, path=path))

    for adapter in selected:
        try:
            inventory = apply_group_rules(adapter.discover(), rules or {})
        except (SystemExit, OSError, ValueError, subprocess.TimeoutExpired):
            add(adapter.name, "error", "discovery_failed", "Inspect platform configuration locally; raw config is not included.")
            continue
        inventories.extend(asdict(item) for item in inventory)
        identities: dict[str, list[str]] = {}
        for item in inventory:
            identities.setdefault(item.identity, []).append(item.path)
            if item.state == "unknown":
                add(adapter.name, "warning", "unknown_state", item.identity, item.path)
            if not item.path:
                continue
            try:
                metadata = read_frontmatter(Path(item.path))
            except ValueError as exc:
                add(adapter.name, "warning", str(exc), item.identity, item.path)
                continue
            name = metadata.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
                add(adapter.name, "warning", "nonportable_name", "Host may accept this name, but the portable specification does not.", item.path)
            elif name != Path(item.path).parent.name:
                add(adapter.name, "warning", "directory_name_mismatch", "Frontmatter name differs from its directory.", item.path)
            description = metadata.get("description")
            if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                add(adapter.name, "warning", "invalid_description", "Expected a nonempty description of at most 1024 characters.", item.path)
            if metadata.get("disable-model-invocation") is True or metadata.get("user-invocable") is False:
                add(adapter.name, "info", "frontmatter_invocation_policy", "Host-specific invocation flags also influence visibility.", item.path)
        for identity, paths in identities.items():
            if len(paths) > 1:
                add(adapter.name, "warning", "duplicate_identity", f"{identity}: {len(paths)} discovered locations; host precedence is not inferred.")
        if adapter.name == "codex":
            text, config = adapter._load()
            try:
                adapter._managed_rules(text)
            except (SystemExit, ValueError):
                add(adapter.name, "error", "invalid_managed_block", "Manager-owned rules require inspection; no changes made.")
            for rule in adapter._entries(config):
                path = rule.get("path")
                if isinstance(path, str) and not Path(path).expanduser().exists():
                    add(adapter.name, "warning", "stale_rule_path", "Configured path is absent; re-scan after installation or plugin changes.", path)
            if any("name" in rule for rule in adapter._entries(config)):
                add(adapter.name, "warning", "name_rule_host_dependent", "Public Codex docs specify path rules. Verify name rules against your exact host build.")
            add(adapter.name, "info", "plugin_cache_not_registry", "Cache discovery may include stale versions; plugin activation and prompt metadata require host evidence.")
        if adapter.name in {"claude", "copilot"}:
            folder = ".claude" if adapter.name == "claude" else ".copilot"
            for parent in (adapter.cwd.resolve(), *adapter.cwd.resolve().parents):
                for filename in ("settings.json", "settings.local.json"):
                    path = parent / folder / filename
                    if path.is_file() and path.resolve() != adapter.config.resolve():
                        add(adapter.name, "warning", "additional_config_layer", "This configuration may change effective host state; this adapter writes only its selected config.", str(path))
                if (parent / ".git").exists():
                    break
        if adapter.name == "antigravity":
            add(adapter.name, "warning", "parking_scope_only", "Only the selected global root is managed. Workspace/plugin copies may remain available; prompt visibility is unverified.")
            active = {item.config_key for item in adapter._entries(adapter.active_root, "on")}
            parked = {item.config_key for item in adapter._entries(adapter.disabled_root, "off")}
            for identity in sorted(active & parked):
                add(adapter.name, "warning", "parking_collision", f"{identity} exists in both active and parked roots.")
    return {
        "schema_version": 1,
        "evidence_scope": "local configuration and installed metadata; no live prompt verification",
        "findings": findings,
        "skills": inventories,
        "summary": {level: sum(item["severity"] == level for item in findings) for level in ("error", "warning", "info")},
    }


def load_preset(path: Path, name: str, platform: str) -> dict[str, list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Preset file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid preset JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise SystemExit("Preset file version must be 1.")
    try:
        value = document["presets"][name][platform]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"Preset {name!r} has no {platform!r} configuration.") from exc
    if not isinstance(value, dict):
        raise SystemExit("Platform preset must be an object.")
    if set(value) - {"disable", "enable"}:
        raise SystemExit("Unknown preset action; supported actions are disable and enable.")
    result: dict[str, list[str]] = {}
    for action in ("disable", "enable"):
        selectors = value.get(action, [])
        if not isinstance(selectors, list) or not all(
            isinstance(item, str) for item in selectors
        ):
            raise SystemExit(f"Preset {action} must be an array of strings.")
        result[action] = selectors
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and toggle installed Agent Skills without deleting files."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--platform", choices=("all", *PLATFORMS), default="all"
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--codex-selector", choices=("path", "name"), default="path",
                        help="Use documented paths (default); name requires host-specific verification.")
    parser.add_argument("--claude-config", type=Path)
    parser.add_argument("--copilot-config", type=Path)
    parser.add_argument("--openclaw-config", type=Path)
    parser.add_argument("--hermes-config", type=Path)
    parser.add_argument(
        "--antigravity-surface", choices=("app", "cli"), default="app",
        help="App/IDE global root or current agy CLI global root.",
    )
    parser.add_argument(
        "--antigravity-config",
        type=Path,
        help="Antigravity config directory holding skills/ (default ~/.gemini/config).",
    )
    parser.add_argument(
        "--groups",
        type=Path,
        help="Optional JSON file defining your own group:<name> taxonomy.",
    )
    parser.add_argument(
        "--hermes-scope",
        default="global",
        help="Hermes disable scope: global or a platform name such as cli/telegram.",
    )
    json_help = "Emit machine-readable JSON; accepted before or after the command."
    parser.add_argument("--json", action="store_true", help=json_help)
    parser.add_argument("--allow-undiscovered", action="store_true",
                        help="Allow literal unknown names for Copilot/OpenClaw/Hermes; no fuzzy matching.")

    def add_command_json(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help=json_help,
        )

    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    add_command_json(doctor)
    status = subparsers.add_parser("status")
    add_command_json(status)
    audit = subparsers.add_parser("audit", help="Read-only metadata and compatibility diagnostics.")
    audit.add_argument("--strict", action="store_true", help="Return 1 on warnings as well as errors.")
    add_command_json(audit)
    search = subparsers.add_parser("search")
    search.add_argument("query")
    add_command_json(search)
    reconcile = subparsers.add_parser(
        "reconcile",
        help="Normalize Codex Skill configuration without modifying installed files.",
    )
    reconcile.add_argument("--dry-run", action="store_true")
    add_command_json(reconcile)
    for action in ("enable", "disable"):
        mutation = subparsers.add_parser(action)
        mutation.add_argument("selectors", nargs="+")
        mutation.add_argument("--dry-run", action="store_true")
        mutation.add_argument("--force", action="store_true")
        add_command_json(mutation)
    setter = subparsers.add_parser(
        "set", help="Set a Claude Code skill visibility state."
    )
    setter.add_argument("state", choices=SET_STATES)
    setter.add_argument("selectors", nargs="+")
    setter.add_argument("--dry-run", action="store_true")
    setter.add_argument("--force", action="store_true")
    add_command_json(setter)
    preset = subparsers.add_parser("preset")
    preset.add_argument("name")
    preset.add_argument("--file", type=Path, required=True)
    preset.add_argument("--dry-run", action="store_true")
    preset.add_argument("--force", action="store_true")
    add_command_json(preset)
    return parser


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, dict) and "platforms" in value:
        for row in value["platforms"]:
            counts = " ".join(f"{state}={row[state]:3}" for state in STATES)
            print(
                f"{row['platform']:8} available={str(row['available']).lower():5} "
                f"discovered={row['discovered']:3} {counts} "
                f"context-visible={row['context_visible']:3} "
                f"context-hidden={row['context_hidden']:3} "
                f"context-unverified={row['context_unverified']:3} "
                f"context-drift={row['context_drift']:3} {row['detail']}"
            )
            if row.get("error"):
                print(f"  error={row['error']}")
        return
    if isinstance(value, dict) and "findings" in value:
        for row in value["findings"]:
            print(f"{row['platform']:12} {row['severity']:7} {row['code']}: {row['detail']} {row['path']}")
        print(f"summary={value['summary']}")
        return
    if isinstance(value, list):
        for row in value:
            key = row.get("config_key", "")
            # Surface the host's own identifier whenever the label differs.
            label = (
                f"{row['name']} (key={key})"
                if key and key.lower() != row["name"].lower()
                else row["name"]
            )
            print(
                f"{row['platform']:8} {row['state']:10} {label} "
                f"[{row['group']}] {row['path']}"
            )
        print(f"matches={len(value)}")
        return
    for key, item in value.items():
        print(f"{key}={item}")


def main() -> int:
    args = build_parser().parse_args()
    selected = adapters(args)
    rules = load_group_rules(args.groups)
    if args.command in {"doctor", "status"}:
        payload = status_payload(selected, rules)
        emit(payload, args.json)
        return int(any(row["error"] for row in payload["platforms"]))
    if args.command == "audit":
        payload = audit_payload(selected, rules)
        emit(payload, args.json)
        return int(any(row["severity"] == "error" or (args.strict and row["severity"] == "warning") for row in payload["findings"]))
    if args.command == "search":
        query = args.query.lower()
        rows = [
            asdict(item)
            for adapter in selected
            for item in apply_group_rules(adapter.discover(), rules)
            if query in item.name.lower()
            or query in item.identity.lower()
            or query in item.group.lower()
            or query in item.path.lower()
        ]
        emit(rows, args.json)
        return 0
    if args.platform == "all":
        raise SystemExit("Mutations require an explicit --platform.")
    adapter = selected[0]
    if args.command == "reconcile":
        if args.platform != "codex":
            raise SystemExit("reconcile is currently supported only for Codex.")
        emit(adapter.reconcile(args.dry_run), args.json)
        return 0
    inventory = apply_group_rules(adapter.discover(), rules)
    if args.command == "preset":
        preset = load_preset(args.file, args.name, args.platform)
        resolved = {
            action: resolve_selectors(preset[action], inventory)
            if preset[action]
            else []
            for action in ("disable", "enable")
        }
        # Enables win without transiently disabling the same host identity.
        def mutation_key(item: Skill) -> str:
            return item.key if args.platform == "codex" and args.codex_selector == "path" else item.identity
        enabled_ids = {mutation_key(item) for item in resolved["enable"]}
        resolved["disable"] = [item for item in resolved["disable"] if mutation_key(item) not in enabled_ids]
        # Validate every operation before the first side effect.
        for action in ("disable", "enable"):
            if resolved[action]:
                adapter.mutate(action, resolved[action], True, args.force)
        results = []
        for action in ("disable", "enable"):
            if not resolved[action]:
                continue
            results.append(
                adapter.mutate(action, resolved[action], args.dry_run, args.force)
            )
        emit({"preset": args.name, "results": results}, args.json)
        return 0
    action = args.state if args.command == "set" else args.command
    try:
        targets = resolve_selectors(args.selectors, inventory)
    except SystemExit:
        if not args.allow_undiscovered or args.platform not in {"copilot", "openclaw", "hermes"} or any(
            value.lower() == "all"
            or value.lower().startswith(("group:", "path:"))
            for value in args.selectors
        ):
            raise
        targets = [
            Skill(args.platform, name, "", args.platform, "explicit-selector")
            for name in args.selectors
        ]
    result = adapter.mutate(action, targets, args.dry_run, args.force)
    emit(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
