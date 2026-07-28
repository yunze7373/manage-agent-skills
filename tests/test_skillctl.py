from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "manage-agent-skills"
    / "scripts"
    / "skillctl.py"
)
SPEC = importlib.util.spec_from_file_location("skillctl", SCRIPT)
assert SPEC and SPEC.loader
skillctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = skillctl
SPEC.loader.exec_module(skillctl)


def make_skill(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: Test {name}.\n---\n", encoding="utf-8"
    )
    return skill_file


class SelectorTests(unittest.TestCase):
    def test_exact_names_and_groups(self) -> None:
        inventory = [
            skillctl.Skill("codex", "alpha", "C:/a/SKILL.md", "one", "test"),
            skillctl.Skill("codex", "beta", "C:/b/SKILL.md", "two", "test"),
        ]
        self.assertEqual(
            ["alpha"], [item.name for item in skillctl.resolve_selectors(["alpha"], inventory)]
        )
        self.assertEqual(
            ["beta"],
            [item.name for item in skillctl.resolve_selectors(["group:two"], inventory)],
        )
        with self.assertRaises(SystemExit):
            skillctl.resolve_selectors(["alp"], inventory)


class CodexAdapterTests(unittest.TestCase):
    def test_disable_enable_roundtrip_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.toml"
            config.write_text('model = "test"\n', encoding="utf-8")
            skill_file = make_skill(base / "skills", "demo")
            adapter = skillctl.CodexAdapter(base, config)
            target = skillctl.Skill(
                "codex", "demo", str(skill_file), "test", "test", "on"
            )

            disabled = adapter.mutate("disable", [target], False, False)
            self.assertTrue(disabled["changed"])
            text = config.read_text(encoding="utf-8")
            self.assertIn(skillctl.CODEX_BEGIN, text)
            self.assertIn(str(skill_file), text)
            self.assertTrue(
                config.with_name(config.name + ".manage-agent-skills.bak").exists()
            )

            enabled = adapter.mutate("enable", [target], False, False)
            self.assertTrue(enabled["changed"])
            self.assertNotIn(str(skill_file), config.read_text(encoding="utf-8"))

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.toml"
            config.write_text("", encoding="utf-8")
            skill_file = make_skill(base / "skills", "demo")
            adapter = skillctl.CodexAdapter(base, config)
            target = skillctl.Skill(
                "codex", "demo", str(skill_file), "test", "test", "on"
            )
            result = adapter.mutate("disable", [target], True, False)
            self.assertTrue(result["changed"])
            self.assertEqual("", config.read_text(encoding="utf-8"))

    def test_external_disable_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skill_file = make_skill(base / "skills", "demo")
            config = base / "config.toml"
            config.write_text(
                "[[skills.config]]\n"
                f"path = '{skill_file}'\n"
                "enabled = false\n",
                encoding="utf-8",
            )
            adapter = skillctl.CodexAdapter(base, config)
            target = skillctl.Skill(
                "codex", "demo", str(skill_file), "test", "test", "off"
            )
            with self.assertRaises(SystemExit):
                adapter.mutate("enable", [target], False, False)

    def test_external_name_disable_is_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skill_file = make_skill(base / "skills", "demo")
            config = base / "config.toml"
            original = (
                "[[skills.config]]\n"
                "name = 'demo'\n"
                "enabled = false\n"
            )
            config.write_text(original, encoding="utf-8")
            adapter = skillctl.CodexAdapter(base, config)
            target = skillctl.Skill(
                "codex", "demo", str(skill_file), "test", "test", "off"
            )
            result = adapter.mutate("disable", [target], False, False)
            self.assertFalse(result["changed"])
            updated = config.read_text(encoding="utf-8")
            self.assertEqual(1, updated.count("name = 'demo'"))
            self.assertNotIn(str(skill_file), updated)
            with self.assertRaises(SystemExit):
                adapter.mutate("enable", [target], False, False)

    def test_legacy_managed_block_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skill_file = make_skill(base / "skills", "demo")
            config = base / "config.toml"
            config.write_text(
                f"{skillctl.CODEX_LEGACY_BEGIN}\n"
                "[[skills.config]]\n"
                f"path = '{skill_file}'\n"
                "enabled = false\n"
                f"{skillctl.CODEX_LEGACY_END}\n",
                encoding="utf-8",
            )
            adapter = skillctl.CodexAdapter(base, config)
            target = skillctl.Skill(
                "codex", "demo", str(skill_file), "test", "test", "off"
            )
            result = adapter.mutate("enable", [target], False, False)
            self.assertTrue(result["changed"])
            updated = config.read_text(encoding="utf-8")
            self.assertIn(skillctl.CODEX_BEGIN, updated)
            self.assertNotIn(skillctl.CODEX_LEGACY_BEGIN, updated)
            self.assertNotIn(str(skill_file), updated)


class ClaudeAdapterTests(unittest.TestCase):
    def test_only_skill_overrides_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "settings.json"
            config.write_text(
                json.dumps({"model": "test", "skillOverrides": {"old": "off"}}),
                encoding="utf-8",
            )
            skill_file = make_skill(base / ".claude" / "skills", "demo")
            adapter = skillctl.ClaudeAdapter(base, config)
            target = skillctl.Skill(
                "claude", "demo", str(skill_file), "test", "test", "on"
            )
            adapter.mutate("disable", [target], False, False)
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("test", value["model"])
            self.assertEqual("off", value["skillOverrides"]["old"])
            self.assertEqual("off", value["skillOverrides"]["demo"])

            adapter.mutate("enable", [target], False, False)
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("on", value["skillOverrides"]["demo"])


class CopilotParserTests(unittest.TestCase):
    def test_parses_nested_inventory(self) -> None:
        payload = {
            "resources": [
                {
                    "kind": "skill",
                    "name": "demo",
                    "path": "/tmp/demo/SKILL.md",
                    "enabled": False,
                    "scope": "user",
                }
            ]
        }
        inventory = skillctl.parse_copilot_inventory(payload)
        self.assertEqual(1, len(inventory))
        self.assertEqual("demo", inventory[0].name)
        self.assertEqual("off", inventory[0].state)

    def test_jsonc_and_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "settings.json"
            config.write_text(
                '{\n  // retained value\n  "model": "test",\n'
                '  "disabledSkills": ["old",],\n}\n',
                encoding="utf-8",
            )
            adapter = skillctl.CopilotAdapter(base, config)
            target = skillctl.Skill(
                "copilot", "demo", "", "test", "test", "on"
            )
            adapter.mutate("disable", [target], False, False)
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("test", value["model"])
            self.assertEqual(["demo", "old"], value["disabledSkills"])
            adapter.mutate("enable", [target], False, False)
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(["old"], value["disabledSkills"])


class OpenClawAdapterTests(unittest.TestCase):
    def test_fallback_discovery_uses_metadata_skill_key_and_config_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            skill_file = make_skill(base / "skills", "demo")
            skill_file.write_text(
                "---\n"
                "name: demo\n"
                "description: Test demo.\n"
                'metadata: {"openclaw":{"skillKey":"demo-runtime"}}\n'
                "---\n",
                encoding="utf-8",
            )
            config = base / "openclaw.json"
            config.write_text(
                json.dumps(
                    {
                        "skills": {
                            "entries": {"demo-runtime": {"enabled": False}}
                        }
                    }
                ),
                encoding="utf-8",
            )
            adapter = skillctl.OpenClawAdapter(base, config)
            adapter.executable = None

            inventory = adapter.discover()
            demo = next(item for item in inventory if item.name == "demo")
            self.assertEqual("demo-runtime", demo.config_key)
            self.assertEqual("off", demo.state)

    def test_dry_run_delegates_to_native_config_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "openclaw.json"
            config.write_text("{}\n", encoding="utf-8")
            adapter = skillctl.OpenClawAdapter(base, config)
            adapter.executable = None
            target = skillctl.Skill(
                "openclaw",
                "demo",
                "",
                "openclaw",
                "test",
                "on",
                "demo-runtime",
            )

            result = adapter.mutate("disable", [target], True, False)
            self.assertTrue(result["changed"])
            self.assertEqual(
                [
                    "openclaw",
                    "config",
                    "set",
                    'skills.entries["demo-runtime"].enabled',
                    "false",
                ],
                result["commands"][0],
            )
            self.assertEqual("{}\n", config.read_text(encoding="utf-8"))

    def test_native_mutation_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "openclaw.json"
            config.write_text('{"unrelated": true}\n', encoding="utf-8")
            adapter = skillctl.OpenClawAdapter(base, config)
            adapter.executable = "openclaw"
            target = skillctl.Skill(
                "openclaw", "demo", "", "openclaw", "test", "on", "demo"
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(skillctl.subprocess, "run", return_value=completed) as run:
                result = adapter.mutate("disable", [target], False, False)

            self.assertTrue(result["changed"])
            self.assertTrue(
                config.with_name(config.name + ".manage-agent-skills.bak").exists()
            )
            self.assertEqual(
                'skills.entries["demo"].enabled', run.call_args.args[0][3]
            )


class HermesAdapterTests(unittest.TestCase):
    def test_global_roundtrip_preserves_unrelated_config_and_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.yaml"
            config.write_text(
                "model: test\nskills:\n  disabled:\n    - old\n",
                encoding="utf-8",
            )
            skill_file = make_skill(base / "skills", "demo")
            adapter = skillctl.HermesAdapter(base, config, "global")
            target = skillctl.Skill(
                "hermes", "demo", str(skill_file), "hermes-home", "test", "on"
            )

            disabled = adapter.mutate("disable", [target], False, False)
            self.assertTrue(disabled["changed"])
            value = skillctl._load_yaml_module().safe_load(
                config.read_text(encoding="utf-8")
            )
            self.assertEqual("test", value["model"])
            self.assertEqual(["demo", "old"], value["skills"]["disabled"])
            self.assertTrue(
                config.with_name(config.name + ".manage-agent-skills.bak").exists()
            )

            enabled = adapter.mutate("enable", [target], False, False)
            self.assertTrue(enabled["changed"])
            value = skillctl._load_yaml_module().safe_load(
                config.read_text(encoding="utf-8")
            )
            self.assertEqual(["old"], value["skills"]["disabled"])

    def test_platform_scope_and_global_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.yaml"
            config.write_text(
                "skills:\n"
                "  disabled:\n"
                "    - global-off\n"
                "  platform_disabled:\n"
                "    telegram:\n"
                "      - existing\n",
                encoding="utf-8",
            )
            adapter = skillctl.HermesAdapter(base, config, "telegram")
            target = skillctl.Skill(
                "hermes", "demo", "", "hermes-home", "test", "on"
            )
            adapter.mutate("disable", [target], False, False)
            value = skillctl._load_yaml_module().safe_load(
                config.read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["demo", "existing"],
                value["skills"]["platform_disabled"]["telegram"],
            )

            globally_disabled = skillctl.Skill(
                "hermes", "global-off", "", "hermes-home", "test", "off"
            )
            with self.assertRaises(SystemExit):
                adapter.mutate("enable", [globally_disabled], False, False)

    def test_dry_run_does_not_create_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.yaml"
            adapter = skillctl.HermesAdapter(base, config, "cli")
            target = skillctl.Skill(
                "hermes", "demo", "", "hermes-home", "test", "on"
            )
            result = adapter.mutate("disable", [target], True, False)
            self.assertTrue(result["changed"])
            self.assertFalse(config.exists())

    def test_native_scalar_and_null_disabled_values_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "config.yaml"
            config.write_text(
                "skills:\n"
                "  disabled: global-off\n"
                "  platform_disabled:\n"
                "    cli:\n",
                encoding="utf-8",
            )
            make_skill(base / "skills", "global-off")
            adapter = skillctl.HermesAdapter(base, config, "cli")
            inventory = adapter.discover()
            self.assertEqual("off", inventory[0].state)


class PresetTests(unittest.TestCase):
    def test_loads_platform_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "presets": {
                            "lean": {
                                "codex": {
                                    "disable": ["group:test"],
                                    "enable": ["manager"],
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            value = skillctl.load_preset(path, "lean", "codex")
            self.assertEqual(["group:test"], value["disable"])
            self.assertEqual(["manager"], value["enable"])


if __name__ == "__main__":
    unittest.main()
