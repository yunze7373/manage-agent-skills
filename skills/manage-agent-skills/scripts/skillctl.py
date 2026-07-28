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
from pathlib import Path
from typing import Any, Iterable


CODEX_BEGIN = "# BEGIN MANAGE_AGENT_SKILLS:CODEX"
CODEX_END = "# END MANAGE_AGENT_SKILLS:CODEX"
CODEX_LEGACY_BEGIN = "# BEGIN CODEX_SKILL_MANAGER"
CODEX_LEGACY_END = "# END CODEX_SKILL_MANAGER"
FRONTMATTER_NAME = re.compile(r"(?m)^name:\s*[\"']?([^\"'\r\n]+)")
OPENCLAW_SKILL_KEY = re.compile(
    r"""(?:["']?skillKey["']?\s*:\s*)["']?([^"',}\]\r\n]+)"""
)
PROTECTED_NAMES = {
    "manage-agent-skills",
    "manage-codex-skills",
    "plugin-creator",
    "skill-creator",
}
PLATFORMS = ("codex", "claude", "copilot", "openclaw", "hermes")


@dataclass(frozen=True)
class Skill:
    platform: str
    name: str
    path: str
    group: str
    origin: str
    state: str = "unknown"
    config_key: str = ""

    @property
    def key(self) -> str:
        return path_key(self.path) if self.path else self.name.lower()


def path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def read_skill_name(skill_file: Path) -> str:
    try:
        head = skill_file.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return skill_file.parent.name
    match = FRONTMATTER_NAME.search(head)
    return match.group(1).strip() if match else skill_file.parent.name


def read_openclaw_skill_key(skill_file: Path, fallback: str) -> str:
    try:
        head = skill_file.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return fallback
    match = OPENCLAW_SKILL_KEY.search(head)
    return match.group(1).strip() if match else fallback


def scan_roots(platform: str, roots: Iterable[tuple[Path, str, str]]) -> list[Skill]:
    found: dict[str, Skill] = {}
    for root, group, origin in roots:
        if not root.is_dir():
            continue
        for skill_file in root.rglob("SKILL.md"):
            key = path_key(skill_file)
            if key in found:
                continue
            name = read_skill_name(skill_file)
            effective_group = group
            if group == "shared" and (
                name.startswith(("azure-", "entra-"))
                or name
                in {
                    "airunway-aks-setup",
                    "appinsights-instrumentation",
                    "microsoft-foundry",
                }
            ):
                effective_group = "azure"
            found[key] = Skill(
                platform=platform,
                name=name,
                path=str(skill_file.resolve()),
                group=effective_group,
                origin=origin,
            )
    return sorted(found.values(), key=lambda item: (item.group, item.name, item.key))


def project_skill_roots(cwd: Path, directory: str, platform: str) -> list[tuple[Path, str, str]]:
    roots: list[tuple[Path, str, str]] = []
    current = cwd.resolve()
    while True:
        root = current / directory / "skills"
        roots.append((root, f"{platform}-project", f"project:{current}"))
        if current.parent == current:
            break
        current = current.parent
    return roots


def write_atomic(path: Path, content: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".manage-agent-skills.bak")
    backup_result: Path | None = None
    if path.exists():
        shutil.copy2(path, backup)
        backup_result = backup
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
            matches = [item for item in inventory if item.name.lower() == lowered]
        if not matches:
            missing.append(selector)
        for item in matches:
            selected[item.key] = item
    if missing:
        terms = [term.lower().removeprefix("group:") for term in missing]
        suggestions = sorted(
            {
                item.name
                for item in inventory
                if any(term in item.name.lower() for term in terms)
            }
        )[:10]
        suffix = f" Suggestions: {', '.join(suggestions)}" if suggestions else ""
        raise SystemExit(f"No exact skill match: {', '.join(missing)}.{suffix}")
    return list(selected.values())


class CodexAdapter:
    name = "codex"

    def __init__(self, cwd: Path, config: Path | None = None) -> None:
        del cwd
        configured_home = os.environ.get("CODEX_HOME")
        self.home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        self.config = config or self.home / "config.toml"

    def available(self) -> tuple[bool, str]:
        return True, str(self.config)

    def _roots(self) -> list[tuple[Path, str, str]]:
        roots = [
            (Path.home() / ".agents" / "skills", "shared", "user:agents"),
            (self.home / "skills", "codex", "user:codex"),
        ]
        cache = self.home / "plugins" / "cache"
        if cache.is_dir():
            for marketplace in cache.iterdir():
                if not marketplace.is_dir():
                    continue
                for plugin in marketplace.iterdir():
                    if plugin.is_dir():
                        roots.append(
                            (plugin, plugin.name.lower(), f"plugin:{marketplace.name}")
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
        entries = parsed.get("skills", {}).get("config", [])
        if not isinstance(entries, list):
            raise SystemExit("Codex skills.config must be an array of tables.")
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _managed_span(text: str) -> tuple[int, int, str] | None:
        found: list[tuple[int, int, str]] = []
        for begin, end in (
            (CODEX_BEGIN, CODEX_END),
            (CODEX_LEGACY_BEGIN, CODEX_LEGACY_END),
        ):
            start = text.find(begin)
            if start < 0:
                continue
            finish_marker = text.find(end, start + len(begin))
            if finish_marker < 0:
                raise SystemExit(f"Found {begin!r} without {end!r}.")
            finish = finish_marker + len(end)
            if text.find(begin, finish) >= 0:
                raise SystemExit(f"Multiple {begin!r} blocks found.")
            found.append((start, finish, text[start:finish]))
        if len(found) > 1:
            raise SystemExit("Both current and legacy managed Codex blocks exist.")
        return found[0] if found else None

    def _managed_paths(self, text: str) -> set[str]:
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
        return {
            path_key(entry["path"])
            for entry in self._entries(parsed)
            if entry.get("enabled") is False and isinstance(entry.get("path"), str)
        }

    def discover(self) -> list[Skill]:
        inventory = scan_roots(self.name, self._roots())
        text, parsed = self._load()
        disabled_paths = {
            path_key(entry["path"])
            for entry in self._entries(parsed)
            if entry.get("enabled") is False and isinstance(entry.get("path"), str)
        }
        disabled_names = {
            entry["name"].lower()
            for entry in self._entries(parsed)
            if entry.get("enabled") is False and isinstance(entry.get("name"), str)
        }
        return [
            Skill(
                **{
                    **asdict(item),
                    "state": "off"
                    if item.key in disabled_paths or item.name.lower() in disabled_names
                    else "on",
                }
            )
            for item in inventory
        ]

    def _render(self, paths: set[str], inventory: list[Skill]) -> str:
        by_key = {item.key: item.path for item in inventory}
        lines = [
            CODEX_BEGIN,
            "# Managed by manage-agent-skills. Skill files remain installed.",
            "",
        ]
        for key in sorted(paths):
            value = by_key.get(key, key)
            if "'" in value:
                raise SystemExit(f"Unsupported single quote in skill path: {value}")
            lines.extend(
                ["[[skills.config]]", f"path = '{value}'", "enabled = false", ""]
            )
        lines.append(CODEX_END)
        return "\n".join(lines)

    def _replace(self, text: str, paths: set[str], inventory: list[Skill]) -> str:
        span = self._managed_span(text)
        if not span and not paths:
            return text
        block = self._render(paths, inventory)
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

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        text, parsed = self._load()
        managed = self._managed_paths(text)
        all_disabled_paths = {
            path_key(entry["path"])
            for entry in self._entries(parsed)
            if entry.get("enabled") is False and isinstance(entry.get("path"), str)
        }
        all_disabled_names = {
            entry["name"].lower()
            for entry in self._entries(parsed)
            if entry.get("enabled") is False and isinstance(entry.get("name"), str)
        }
        external = all_disabled_paths - managed
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
            desired = managed | {
                item.key
                for item in targets
                if item.key not in external
                and item.name.lower() not in all_disabled_names
            }
        else:
            blocked = sorted(
                {
                    item.name
                    for item in targets
                    if item.key in external
                    or item.name.lower() in all_disabled_names
                }
            )
            if blocked:
                raise SystemExit(
                    "Skills disabled outside this manager's block were preserved: "
                    + ", ".join(blocked)
                )
            desired = managed - {item.key for item in targets}
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
            "new_session_recommended": changed and not dry_run,
        }


class ClaudeAdapter:
    name = "claude"

    def __init__(self, cwd: Path, config: Path | None = None) -> None:
        self.cwd = cwd
        self.config = config or Path.home() / ".claude" / "settings.json"

    def available(self) -> tuple[bool, str]:
        return True, str(self.config)

    def _roots(self) -> list[tuple[Path, str, str]]:
        return [
            (Path.home() / ".claude" / "skills", "claude-user", "user:claude"),
            *project_skill_roots(self.cwd, ".claude", "claude"),
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

    def discover(self) -> list[Skill]:
        settings = self._load()
        overrides = settings.get("skillOverrides", {})
        return [
            Skill(
                **{
                    **asdict(item),
                    "state": "off"
                    if overrides.get(item.name) == "off"
                    else "manual"
                    if overrides.get(item.name) == "user-invocable-only"
                    else "on",
                }
            )
            for item in scan_roots(self.name, self._roots())
        ]

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
        if action == "disable":
            protected = sorted(
                {item.name for item in targets if item.name in PROTECTED_NAMES}
            )
            if protected and not force:
                raise SystemExit(
                    "Protected skills require --force: " + ", ".join(protected)
                )
        settings = self._load()
        overrides = dict(settings.get("skillOverrides", {}))
        value = "off" if action == "disable" else "on"
        before = dict(overrides)
        for item in targets:
            overrides[item.name] = value
        settings["skillOverrides"] = dict(sorted(overrides.items()))
        changed = overrides != before
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
            (Path.home() / ".copilot" / "skills", "copilot-user", "user:copilot"),
            (Path.home() / ".agents" / "skills", "shared", "user:agents"),
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
                capture_output=True,
                text=True,
                check=False,
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
                    "state": "off" if item.name.lower() in disabled else "on",
                }
            )
            for item in inventory
        ]

    def mutate(
        self, action: str, targets: list[Skill], dry_run: bool, force: bool
    ) -> dict[str, Any]:
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
        environment = os.environ.copy()
        environment["OPENCLAW_CONFIG_PATH"] = str(self.config)
        return environment

    def _native_json(self, *arguments: str) -> Any | None:
        if not self.executable:
            return None
        result = subprocess.run(
            [self.executable, *arguments],
            cwd=self.cwd,
            env=self._environment(),
            capture_output=True,
            text=True,
            check=False,
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

    def _roots(self, skills_config: dict[str, Any]) -> list[tuple[Path, str, str]]:
        roots = [
            (self.cwd / "skills", "openclaw-workspace", f"workspace:{self.cwd}"),
            (self.cwd / ".agents" / "skills", "shared-project", f"workspace:{self.cwd}"),
            (Path.home() / ".agents" / "skills", "shared", "user:agents"),
            (self.home / "skills", "openclaw-user", "user:openclaw"),
            (self.home / "workspace" / "skills", "openclaw-workspace", "default-workspace"),
            (
                self.home / "workspace" / ".agents" / "skills",
                "shared-project",
                "default-workspace",
            ),
        ]
        load = skills_config.get("load", {})
        extra_dirs = load.get("extraDirs", []) if isinstance(load, dict) else []
        if isinstance(extra_dirs, list):
            for value in extra_dirs:
                if isinstance(value, str):
                    roots.append(
                        (Path(value).expanduser(), "openclaw-extra", "skills.load.extraDirs")
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
                backup_path = self.config.with_name(
                    self.config.name + ".manage-agent-skills.bak"
                )
                shutil.copy2(self.config, backup_path)
                backup = backup_path
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=self.cwd,
                    env=self._environment(),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    message = result.stderr.strip() or result.stdout.strip()
                    raise SystemExit(
                        f"OpenClaw config command failed ({result.returncode}): {message}"
                    )
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
            "Hermes support requires PyYAML. Install requirements.txt first."
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
        inventory = scan_roots(
            self.name,
            [(self.home / "skills", "hermes-home", f"home:{self.home}")],
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


def adapters(args: argparse.Namespace) -> list[Any]:
    selected = PLATFORMS if args.platform == "all" else (args.platform,)
    result: list[Any] = []
    for name in selected:
        if name == "codex":
            result.append(CodexAdapter(args.cwd, args.codex_config))
        elif name == "claude":
            result.append(ClaudeAdapter(args.cwd, args.claude_config))
        elif name == "copilot":
            result.append(CopilotAdapter(args.cwd, args.copilot_config))
        elif name == "openclaw":
            result.append(OpenClawAdapter(args.cwd, args.openclaw_config))
        else:
            result.append(
                HermesAdapter(args.cwd, args.hermes_config, args.hermes_scope)
            )
    return result


def status_payload(selected: list[Any]) -> dict[str, Any]:
    platforms: list[dict[str, Any]] = []
    for adapter in selected:
        available, detail = adapter.available()
        inventory = adapter.discover()
        counts = {
            state: sum(item.state == state for item in inventory)
            for state in ("on", "off", "manual", "unknown")
        }
        platforms.append(
            {
                "platform": adapter.name,
                "available": available,
                "detail": detail,
                "discovered": len(inventory),
                **counts,
            }
        )
    return {"platforms": platforms}


def load_preset(path: Path, name: str, platform: str) -> dict[str, list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Preset file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid preset JSON: {exc}") from exc
    if document.get("version") != 1:
        raise SystemExit("Preset file version must be 1.")
    try:
        value = document["presets"][name][platform]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"Preset {name!r} has no {platform!r} configuration.") from exc
    if not isinstance(value, dict):
        raise SystemExit("Platform preset must be an object.")
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
    parser.add_argument(
        "--platform", choices=("all", *PLATFORMS), default="all"
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--claude-config", type=Path)
    parser.add_argument("--copilot-config", type=Path)
    parser.add_argument("--openclaw-config", type=Path)
    parser.add_argument("--hermes-config", type=Path)
    parser.add_argument(
        "--hermes-scope",
        default="global",
        help="Hermes disable scope: global or a platform name such as cli/telegram.",
    )
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    search = subparsers.add_parser("search")
    search.add_argument("query")
    for action in ("enable", "disable"):
        mutation = subparsers.add_parser(action)
        mutation.add_argument("selectors", nargs="+")
        mutation.add_argument("--dry-run", action="store_true")
        mutation.add_argument("--force", action="store_true")
    preset = subparsers.add_parser("preset")
    preset.add_argument("name")
    preset.add_argument("--file", type=Path, required=True)
    preset.add_argument("--dry-run", action="store_true")
    preset.add_argument("--force", action="store_true")
    return parser


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, dict) and "platforms" in value:
        for row in value["platforms"]:
            print(
                f"{row['platform']:8} available={str(row['available']).lower():5} "
                f"discovered={row['discovered']:3} on={row['on']:3} "
                f"off={row['off']:3} manual={row['manual']:3} "
                f"unknown={row['unknown']:3} {row['detail']}"
            )
        return
    if isinstance(value, list):
        for row in value:
            print(
                f"{row['platform']:8} {row['state']:7} {row['name']} "
                f"[{row['group']}] {row['path']}"
            )
        print(f"matches={len(value)}")
        return
    for key, item in value.items():
        print(f"{key}={item}")


def main() -> int:
    args = build_parser().parse_args()
    selected = adapters(args)
    if args.command in {"doctor", "status"}:
        emit(status_payload(selected), args.json)
        return 0
    if args.command == "search":
        query = args.query.lower()
        rows = [
            asdict(item)
            for adapter in selected
            for item in adapter.discover()
            if query in item.name.lower()
            or query in item.group.lower()
            or query in item.path.lower()
        ]
        emit(rows, args.json)
        return 0
    if args.platform == "all":
        raise SystemExit("Mutations require an explicit --platform.")
    adapter = selected[0]
    inventory = adapter.discover()
    if args.command == "preset":
        preset = load_preset(args.file, args.name, args.platform)
        resolved = {
            action: resolve_selectors(preset[action], inventory)
            if preset[action]
            else []
            for action in ("disable", "enable")
        }
        results = []
        for action in ("disable", "enable"):
            if not resolved[action]:
                continue
            results.append(
                adapter.mutate(action, resolved[action], args.dry_run, args.force)
            )
        emit({"preset": args.name, "results": results}, args.json)
        return 0
    try:
        targets = resolve_selectors(args.selectors, inventory)
    except SystemExit:
        if args.platform not in {"copilot", "openclaw", "hermes"} or any(
            value == "all" or value.startswith(("group:", "path:"))
            for value in args.selectors
        ):
            raise
        targets = [
            Skill(args.platform, name, "", args.platform, "explicit-selector")
            for name in args.selectors
        ]
    result = adapter.mutate(args.command, targets, args.dry_run, args.force)
    emit(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
