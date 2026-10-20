"""Regression tests for configuration safety and evidence-bound diagnostics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_skillctl import make_skill, skillctl


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()

    def test_frontmatter_ignores_body_and_yaml_comment(self):
        path = make_skill(self.root, "actual")
        path.write_text('---\nname: actual # comment\ndescription: >-\n  multiline text\n---\nname: attacker\n', encoding="utf-8-sig")
        self.assertEqual("actual", skillctl.read_skill_name(path))
        self.assertEqual("multiline text", skillctl.read_frontmatter(path)["description"])
        path.write_text("# no metadata\nname: attacker\n", encoding="utf-8")
        self.assertEqual("actual", skillctl.read_skill_name(path))

    def test_metadata_requires_mapping_and_closing_fence(self):
        path = make_skill(self.root, "actual")
        for content in ("---\n- list\n---\n", "---\nname: x\n", "---\nname: [\n---\n"):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                skillctl.read_frontmatter(path)

    def test_openclaw_key_is_scoped_to_host_metadata(self):
        path = make_skill(self.root, "demo")
        path.write_text('---\nname: demo\nmetadata:\n  other:\n    skillKey: wrong\n  openclaw:\n    skillKey: correct\n---\nskillKey: body\n', encoding="utf-8")
        self.assertEqual("correct", skillctl.read_openclaw_skill_key(path, "demo"))

    def test_backup_history_survives_repeated_writes(self):
        path = self.root / "settings.json"
        path.write_bytes(b"original")
        first = skillctl.write_atomic(path, "second")
        second = skillctl.write_atomic(path, "third")
        self.assertNotEqual(first, second)
        self.assertEqual(b"original", first.read_bytes())
        self.assertEqual(b"second", second.read_bytes())
        self.assertEqual("third", path.read_text())

    def test_failed_replace_preserves_config_and_backup(self):
        path = self.root / "settings.json"
        path.write_bytes(b"original")
        with mock.patch.object(skillctl.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                skillctl.write_atomic(path, "new")
        self.assertEqual(b"original", path.read_bytes())
        self.assertEqual(b"original", path.with_name(path.name + ".manage-agent-skills.bak").read_bytes())
        self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_doctor_continues_after_invalid_platform_without_config_leak(self):
        broken = mock.Mock(name="broken")
        broken.name = "broken"
        broken.available.return_value = (True, "config")
        broken.discover.side_effect = SystemExit("SECRET in parser error")
        healthy = mock.Mock()
        healthy.name = "healthy"
        healthy.available.return_value = (True, "config")
        healthy.discover.return_value = []
        payload = skillctl.status_payload([broken, healthy])
        self.assertEqual(2, len(payload["platforms"]))
        self.assertIsNotNone(payload["platforms"][0]["error"])
        self.assertIsNone(payload["platforms"][1]["error"])
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_audit_reports_duplicates_and_bad_metadata(self):
        one = make_skill(self.root / "one", "demo")
        two = make_skill(self.root / "two", "demo")
        two.write_text("not a skill", encoding="utf-8")
        adapter = mock.Mock()
        adapter.name = "test"
        adapter.discover.return_value = [
            skillctl.Skill("test", "demo", str(p), "test", "test", "on")
            for p in (one, two)
        ]
        payload = skillctl.audit_payload([adapter])
        codes = {row["code"] for row in payload["findings"]}
        self.assertIn("missing_frontmatter", codes)
        self.assertIn("duplicate_identity", codes)
        self.assertEqual("not a skill", two.read_text())

    def test_audit_detects_claude_project_layer(self):
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir()
        path.write_text('{"skillOverrides":{"demo":"off"}}')
        adapter = skillctl.ClaudeAdapter(self.root, self.root / "user.json")
        with mock.patch.object(adapter, "discover", return_value=[]):
            payload = skillctl.audit_payload([adapter])
        self.assertIn("additional_config_layer", {row["code"] for row in payload["findings"]})

    def test_codex_default_path_and_folder_rule_match(self):
        path = make_skill(self.root / ".agents" / "skills", "demo")
        config = self.root / "config.toml"
        config.write_text(f"[[skills.config]]\npath = '{path.parent}'\nenabled = false\n")
        adapter = skillctl.CodexAdapter(self.root, config)
        target = skillctl.Skill("codex", "demo", str(path), "test", "test")
        self.assertTrue(adapter._item_rules(target) & adapter._disabled_rules(adapter._load()[1]))
        with self.assertRaises(SystemExit):
            adapter.mutate("enable", [target], False, False)

    def test_codex_project_discovery_stops_at_repo(self):
        path = make_skill(self.root / ".agents" / "skills", "demo")
        adapter = skillctl.CodexAdapter(self.root, self.root / "config.toml")
        with mock.patch.object(skillctl.Path, "home", return_value=self.root), mock.patch.dict(skillctl.os.environ, {"CODEX_HOME": str(self.root / ".codex")}):
            adapter = skillctl.CodexAdapter(self.root, self.root / "config.toml")
            self.assertEqual([str(path)], [s.path for s in adapter.discover()])

    def test_codex_rejects_orphan_marker(self):
        with self.assertRaises(SystemExit):
            skillctl.CodexAdapter._managed_span(skillctl.CODEX_END + "\n")

    def test_codex_accepts_apostrophe_in_path(self):
        path = make_skill(self.root / "alice's skills", "demo")
        target = skillctl.Skill("codex", "demo", str(path), "test", "test")
        adapter = skillctl.CodexAdapter(self.root, self.root / "config.toml")
        with mock.patch.object(adapter, "discover", return_value=[target]):
            adapter.mutate("disable", [target], False, False)
        self.assertEqual(str(path), adapter._load()[1]["skills"]["config"][0]["path"])

    def test_codex_reconcile_preserves_paths_by_default(self):
        path = make_skill(self.root / "plugins" / "cache" / "market" / "demo" / "1" / "skills", "demo")
        config = self.root / "config.toml"
        config.write_text(f"{skillctl.CODEX_BEGIN}\n[[skills.config]]\npath = '{path}'\nenabled = false\n{skillctl.CODEX_END}\n")
        adapter = skillctl.CodexAdapter(self.root, config)
        with mock.patch.object(adapter, "discover", return_value=[]):
            result = adapter.reconcile(False)
        self.assertEqual(0, result["rules_migrated"])
        self.assertIn("path", adapter._load()[1]["skills"]["config"][0])

    def test_copilot_custom_home_discovery(self):
        make_skill(self.root / "skills", "demo")
        with mock.patch.dict(skillctl.os.environ, {"COPILOT_HOME": str(self.root)}):
            adapter = skillctl.CopilotAdapter(self.root)
        self.assertTrue(any(s.name == "demo" for s in adapter._fallback()))

    def test_hermes_essential_cannot_be_disabled_even_with_force(self):
        path = make_skill(self.root / "skills", "hermes-agent")
        config = self.root / "config.yaml"
        config.write_text("skills:\n  disabled: [hermes-agent]\n")
        adapter = skillctl.HermesAdapter(self.root, config)
        target = adapter.discover()[0]
        self.assertEqual("on", target.state)
        with self.assertRaises(SystemExit):
            adapter.mutate("disable", [target], False, True)
        self.assertTrue(path.is_file())

    def test_hermes_support_files_are_not_skills(self):
        make_skill(self.root / "skills", "demo")
        make_skill(self.root / "skills" / "demo" / "references", "example")
        adapter = skillctl.HermesAdapter(self.root, self.root / "config.yaml")
        self.assertEqual(["demo"], [s.name for s in adapter.discover()])

    def test_antigravity_current_cli_root(self):
        with mock.patch.object(skillctl.Path, "home", return_value=self.root), mock.patch.dict(skillctl.os.environ, {}, clear=True):
            adapter = skillctl.AntigravityAdapter(self.root, surface="cli")
        self.assertEqual(self.root / ".gemini" / "antigravity-cli" / "skills", adapter.active_root)

    def test_antigravity_incomplete_rollback_is_reported(self):
        make_skill(self.root / "skills", "a")
        make_skill(self.root / "skills", "b")
        adapter = skillctl.AntigravityAdapter(self.root, self.root)
        targets = adapter.discover()
        rename = skillctl.os.rename
        calls = 0

        def fail_after_first(source, dest):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("locked")
            rename(source, dest)

        with mock.patch.object(skillctl.os, "rename", side_effect=fail_after_first):
            with self.assertRaisesRegex(SystemExit, "rollback incomplete"):
                adapter.mutate("disable", targets, False, False)
        self.assertTrue((self.root / "skills_disabled" / "a").is_dir())
        self.assertTrue((self.root / "skills" / "b").is_dir())

    def test_preset_enable_wins_without_transient_move(self):
        make_skill(self.root / "skills", "keep")
        make_skill(self.root / "skills", "park")
        preset = self.root / "preset.json"
        preset.write_text(json.dumps({"version": 1, "presets": {"lean": {"antigravity": {"disable": ["all"], "enable": ["keep"]}}}}))
        argv = ["skillctl", "--platform", "antigravity", "--antigravity-config", str(self.root), "preset", "lean", "--file", str(preset)]
        with mock.patch.object(skillctl.sys, "argv", argv), mock.patch("builtins.print"):
            self.assertEqual(0, skillctl.main())
        self.assertTrue((self.root / "skills" / "keep").is_dir())
        self.assertFalse((self.root / "skills_disabled" / "keep").exists())
        self.assertTrue((self.root / "skills_disabled" / "park").is_dir())

    def test_preset_preflights_later_failure_before_any_mutation(self):
        preset = self.root / "preset.json"
        preset.write_text(json.dumps({"version": 1, "presets": {"lean": {"hermes": {"disable": ["a"], "enable": ["b"]}}}}))
        adapter = mock.Mock()
        adapter.discover.return_value = [skillctl.Skill("hermes", name, "", "test", "test") for name in ("a", "b")]
        calls = []

        def mutate(action, targets, dry_run, force):
            calls.append((action, dry_run))
            if action == "enable":
                raise SystemExit("global disable")
            return {}

        adapter.mutate.side_effect = mutate
        argv = ["skillctl", "--platform", "hermes", "preset", "lean", "--file", str(preset)]
        with mock.patch.object(skillctl.sys, "argv", argv), mock.patch.object(skillctl, "adapters", return_value=[adapter]):
            with self.assertRaises(SystemExit):
                skillctl.main()
        self.assertTrue(all(dry for _, dry in calls))

    def test_preset_rejects_unknown_actions_and_nonobject(self):
        path = self.root / "preset.json"
        for value in ([], {"version": 1, "presets": {"x": {"codex": {"disbale": ["all"]}}}}):
            path.write_text(json.dumps(value))
            with self.assertRaises(SystemExit):
                skillctl.load_preset(path, "x", "codex")

    def test_unknown_name_requires_explicit_opt_in(self):
        adapter = mock.Mock()
        adapter.discover.return_value = []
        argv = ["skillctl", "--platform", "hermes", "disable", "typo"]
        with mock.patch.object(skillctl.sys, "argv", argv), mock.patch.object(skillctl, "adapters", return_value=[adapter]):
            with self.assertRaises(SystemExit):
                skillctl.main()
        adapter.mutate.assert_not_called()

    def test_duplicate_external_codex_rule_still_blocks_enable(self):
        path = make_skill(self.root, "demo")
        rule = f"[[skills.config]]\npath = '{path}'\nenabled = false\n"
        config = self.root / "config.toml"
        config.write_text(rule + skillctl.CODEX_BEGIN + "\n" + rule + skillctl.CODEX_END + "\n")
        adapter = skillctl.CodexAdapter(self.root, config)
        target = skillctl.Skill("codex", "demo", str(path), "test", "test")
        before = config.read_bytes()
        with self.assertRaises(SystemExit):
            adapter.mutate("enable", [target], False, False)
        self.assertEqual(before, config.read_bytes())

    def test_unsupported_managed_rule_is_not_silently_removed(self):
        adapter = skillctl.CodexAdapter(self.root, self.root / "config.toml")
        text = skillctl.CODEX_BEGIN + "\n[[skills.config]]\nname = 'demo'\nenabled = true\n" + skillctl.CODEX_END
        with self.assertRaises(SystemExit):
            adapter._managed_rules(text)

    def test_antigravity_refuses_redirected_parking_root(self):
        make_skill(self.root / "skills", "demo")
        outside = self.root / "outside" / "parked"
        outside.mkdir(parents=True)
        try:
            (self.root / "skills_disabled").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are not permitted on this host")
        adapter = skillctl.AntigravityAdapter(self.root, self.root)
        with self.assertRaisesRegex(SystemExit, "redirected"):
            adapter.mutate("disable", adapter.discover(), False, False)
        self.assertTrue((self.root / "skills" / "demo").is_dir())

    def test_openclaw_failure_reports_partial_changes_without_stderr_secrets(self):
        config = self.root / "openclaw.json"
        config.write_text("{}")
        adapter = skillctl.OpenClawAdapter(self.root, config)
        adapter.executable = "openclaw"
        targets = [skillctl.Skill("openclaw", name, "", "test", "test", "on") for name in ("a", "b")]
        responses = [mock.Mock(returncode=0), mock.Mock(returncode=1, stderr="SECRET", stdout="")]
        with mock.patch.object(skillctl.subprocess, "run", side_effect=responses):
            with self.assertRaises(SystemExit) as error:
                adapter.mutate("disable", targets, False, False)
        self.assertIn("after 1 commands", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))
        self.assertTrue(config.with_name(config.name + ".manage-agent-skills.bak").is_file())

    def test_audit_strict_ignores_informational_findings(self):
        for severity, expected in (("info", 0), ("warning", 1), ("error", 1)):
            argv = ["skillctl", "--platform", "codex", "audit", "--strict", "--json"]
            with mock.patch.object(skillctl.sys, "argv", argv), mock.patch.object(skillctl, "audit_payload", return_value={"findings": [{"severity": severity}]}), mock.patch("builtins.print"):
                self.assertEqual(expected, skillctl.main())

    def test_copilot_directory_inventory_and_native_disabled_state(self):
        path = make_skill(self.root / "skills", "demo")
        payload = [{"name": "demo", "path": str(path.parent), "enabled": False}]
        parsed = skillctl.parse_copilot_inventory(payload)
        self.assertEqual(str(path), parsed[0].path)
        adapter = skillctl.CopilotAdapter(self.root, self.root / "settings.json")
        adapter.executable = "copilot"
        with mock.patch.object(skillctl.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))):
            self.assertEqual("off", adapter.discover()[0].state)

    def test_audit_detects_stale_codex_path_and_invalid_marker(self):
        config = self.root / "config.toml"
        config.write_text(f"[[skills.config]]\npath = '{self.root / 'absent'}'\nenabled = false\n{skillctl.CODEX_END}\n")
        adapter = skillctl.CodexAdapter(self.root, config)
        with mock.patch.object(adapter, "discover", return_value=[]):
            payload = skillctl.audit_payload([adapter])
        codes = {row["code"] for row in payload["findings"]}
        self.assertIn("stale_rule_path", codes)
        self.assertIn("invalid_managed_block", codes)
