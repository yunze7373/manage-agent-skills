from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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


def make_skill(root: Path, name: str, frontmatter_name: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {frontmatter_name or name}\ndescription: Test {name}.\n---\n",
        encoding="utf-8",
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

    def test_exact_path_uses_native_os_rules_and_allows_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill_file = make_skill(Path(temporary) / "skills with spaces", "demo")
            native_path = str(skill_file.resolve())
            inventory = [
                skillctl.Skill("codex", "demo", native_path, "test", "test")
            ]

            selected = skillctl.resolve_selectors(
                [f"path:{native_path}"], inventory
            )

            self.assertEqual(["demo"], [item.name for item in selected])


class GroupRuleTests(unittest.TestCase):
    def test_discovery_groups_carry_no_built_in_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            make_skill(base / "skills", "azure-cost")
            make_skill(base / "skills", "legacy-billing")

            inventory = skillctl.scan_roots(
                "codex",
                [(base / "skills", "shared", "test", skillctl.SKILL_GLOB_DEEP)],
            )

            self.assertEqual({"shared"}, {item.group for item in inventory})

    def test_user_supplied_patterns_define_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            path = base / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {"azure": ["azure-*", "legacy-billing"]},
                    }
                ),
                encoding="utf-8",
            )
            inventory = [
                skillctl.Skill("codex", "azure-cost", "", "shared", "test"),
                skillctl.Skill("codex", "legacy-billing", "", "shared", "test"),
                skillctl.Skill("codex", "unrelated", "", "shared", "test"),
            ]

            grouped = skillctl.apply_group_rules(
                inventory, skillctl.load_group_rules(path)
            )

            self.assertEqual(
                {"azure-cost": "azure", "legacy-billing": "azure", "unrelated": "shared"},
                {item.name: item.group for item in grouped},
            )

    def test_no_group_file_leaves_discovery_groups_untouched(self) -> None:
        inventory = [skillctl.Skill("codex", "demo", "", "shared", "test")]
        self.assertEqual(inventory, skillctl.apply_group_rules(inventory, {}))
        self.assertEqual({}, skillctl.load_group_rules(None))

    def test_matches_directory_name_when_label_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            skill_file = make_skill(base / "skills", "azure-cost", "Azure Cost")
            inventory = [
                skillctl.Skill(
                    "claude", "Azure Cost", str(skill_file), "shared", "test"
                )
            ]

            grouped = skillctl.apply_group_rules(inventory, {"azure": ["azure-*"]})

            self.assertEqual("azure", grouped[0].group)


class CliParserTests(unittest.TestCase):
    def test_json_is_accepted_before_or_after_the_command(self) -> None:
        parser = skillctl.build_parser()

        self.assertTrue(parser.parse_args(["--json", "status"]).json)
        self.assertTrue(parser.parse_args(["status", "--json"]).json)
        self.assertTrue(parser.parse_args(["search", "demo", "--json"]).json)


class ExplicitSelectorFallbackTests(unittest.TestCase):
    def test_reserved_selectors_do_not_fall_back_as_names(self) -> None:
        adapter = mock.Mock()
        adapter.discover.return_value = []

        for selector in ("ALL", "GROUP:missing", "PATH:C:/missing/SKILL.md"):
            with self.subTest(selector=selector):
                with (
                    mock.patch.object(skillctl, "adapters", return_value=[adapter]),
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "skillctl.py",
                            "--platform",
                            "copilot",
                            "disable",
                            selector,
                            "--dry-run",
                        ],
                    ),
                ):
                    with self.assertRaises(SystemExit) as error:
                        skillctl.main()

                self.assertIn("No exact skill match", str(error.exception))
                adapter.mutate.assert_not_called()


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


class ProjectRootTests(unittest.TestCase):
    def test_walk_stops_at_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repo"
            (repository / ".git").mkdir(parents=True)
            nested = repository / "apps" / "web"
            nested.mkdir(parents=True)

            roots = skillctl.project_skill_roots(
                nested, ".claude", "claude", skillctl.SKILL_GLOB_FLAT, True
            )

            self.assertEqual(
                [
                    nested / ".claude" / "skills",
                    repository / "apps" / ".claude" / "skills",
                    repository / ".claude" / "skills",
                ],
                [root for root, _, _, _ in roots],
            )

    def test_walk_without_repository_marker_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            roots = skillctl.project_skill_roots(
                base, ".claude", "claude", skillctl.SKILL_GLOB_FLAT, True
            )
            self.assertEqual(base.anchor, str(roots[-1][0].parents[1]))


class ClaudeAdapterTests(unittest.TestCase):
    @staticmethod
    def isolated_tree(base: Path) -> Any:
        """Keep the developer's real skills out of discovery assertions.

        A temporary directory can sit under the home directory, so the parent
        walk needs a repository marker as well as a redirected home.
        """
        home = base / "home"
        (home / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        (base / ".git").mkdir(exist_ok=True)
        return mock.patch.object(skillctl.Path, "home", return_value=home)

    def test_override_key_is_the_directory_name_not_the_frontmatter_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config = base / "settings.json"
            skill_file = make_skill(
                base / ".claude" / "skills", "memory-manager", "Memory Manager"
            )
            adapter = skillctl.ClaudeAdapter(base, config)

            with self.isolated_tree(base):
                inventory = adapter.discover()
                demo = next(
                    item for item in inventory if item.path == str(skill_file)
                )
                self.assertEqual("Memory Manager", demo.name)
                self.assertEqual("memory-manager", demo.config_key)

                # The command a user types must resolve, the label need not.
                targets = skillctl.resolve_selectors(["memory-manager"], inventory)
                result = adapter.mutate("disable", targets, False, False)

            overrides = json.loads(config.read_text(encoding="utf-8"))[
                "skillOverrides"
            ]
            self.assertEqual({"memory-manager": "off"}, overrides)
            self.assertEqual(
                {"Memory Manager": "memory-manager"},
                result["display_name_differs"],
            )

    def test_all_four_documented_states_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config = base / "settings.json"
            skill_file = make_skill(base / ".claude" / "skills", "demo")
            adapter = skillctl.ClaudeAdapter(base, config)
            target = skillctl.Skill(
                "claude", "demo", str(skill_file), "test", "test", "on", "demo"
            )

            for action, setting, state in (
                ("name-only", "name-only", "name-only"),
                ("user-invocable-only", "user-invocable-only", "user-only"),
                ("user-only", "user-invocable-only", "user-only"),
                ("off", "off", "off"),
                ("on", "on", "on"),
            ):
                with self.subTest(action=action):
                    adapter.mutate(action, [target], False, False)
                    value = json.loads(config.read_text(encoding="utf-8"))
                    self.assertEqual(setting, value["skillOverrides"]["demo"])
                    with self.isolated_tree(base):
                        found = next(
                            item
                            for item in adapter.discover()
                            if item.config_key == "demo"
                        )
                    self.assertEqual(state, found.state)

    def test_unrecognized_override_value_is_reported_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config = base / "settings.json"
            config.write_text(
                json.dumps({"skillOverrides": {"demo": "sometimes"}}),
                encoding="utf-8",
            )
            make_skill(base / ".claude" / "skills", "demo")
            adapter = skillctl.ClaudeAdapter(base, config)

            with self.isolated_tree(base):
                found = next(
                    item for item in adapter.discover() if item.config_key == "demo"
                )

            self.assertEqual("unknown", found.state)

    def test_protection_matches_either_identifier_and_only_hiding_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config = base / "settings.json"
            skill_file = make_skill(
                base / ".claude" / "skills", "manage-agent-skills", "Skill Switchboard"
            )
            adapter = skillctl.ClaudeAdapter(base, config)
            target = skillctl.Skill(
                "claude",
                "Skill Switchboard",
                str(skill_file),
                "test",
                "test",
                "on",
                "manage-agent-skills",
            )

            # The protected identity is the directory name, not the label.
            with self.assertRaises(SystemExit) as error:
                adapter.mutate("off", [target], True, False)
            self.assertIn("manage-agent-skills", str(error.exception))

            # Only states that hide the skill from the menu are protected.
            self.assertTrue(adapter.mutate("name-only", [target], True, False)["changed"])
            self.assertTrue(adapter.mutate("off", [target], True, True)["changed"])

    def test_rejects_unsupported_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            adapter = skillctl.ClaudeAdapter(base, base / "settings.json")
            target = skillctl.Skill("claude", "demo", "", "test", "test", "on", "demo")
            with self.assertRaises(SystemExit) as error:
                adapter.mutate("sometimes", [target], True, False)
            self.assertIn("Unsupported Claude skill state", str(error.exception))

    def test_discovery_ignores_skill_files_nested_inside_a_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            skill_file = make_skill(base / ".claude" / "skills", "demo")
            bundled = skill_file.parent / "references" / "example"
            bundled.mkdir(parents=True)
            (bundled / "SKILL.md").write_text("---\nname: example\n---\n", "utf-8")
            adapter = skillctl.ClaudeAdapter(base, base / "settings.json")

            with self.isolated_tree(base):
                inventory = adapter.discover()

            self.assertEqual([str(skill_file)], [item.path for item in inventory])

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


class GradedStateTests(unittest.TestCase):
    def test_other_platforms_reject_claude_only_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            builders = {
                "codex": skillctl.CodexAdapter(base, base / "config.toml"),
                "copilot": skillctl.CopilotAdapter(base, base / "settings.json"),
                "openclaw": skillctl.OpenClawAdapter(base, base / "openclaw.json"),
                "hermes": skillctl.HermesAdapter(base, base / "config.yaml"),
            }
            target = skillctl.Skill("x", "demo", "", "test", "test", "on")

            for platform, adapter in builders.items():
                with self.subTest(platform=platform):
                    with self.assertRaises(SystemExit) as error:
                        adapter.mutate("name-only", [target], True, False)
                    self.assertIn("only enable and disable", str(error.exception))


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
