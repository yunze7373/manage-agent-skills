from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from test_skillctl import make_skill, skillctl


class DiscoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        (self.cwd / ".git").mkdir()
        patch = mock.patch.object(skillctl.Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                                         "GEMINI_CLI_HOME": str(self.home), "OPENCODE_CONFIG": "", "OPENCODE_CONFIG_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def catalog(self, *items):
        return {"schema_version": 1, "generated_at": "2026-10-23T00:00:00+00:00",
                "evidence_scope": "snapshot", "skills": [asdict(i) for i in items], "errors": []}

    def item(self, name="documents", description="Extract invoice totals 发票识别", state="off"):
        return skillctl.Skill("claude", name, "", "test", "test", state, description=description)

    def run_cli(self, args, selected=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(skillctl.sys, "argv", ["skillctl", *args]))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if selected is not None:
                stack.enter_context(mock.patch.object(skillctl, "adapters", return_value=selected))
            code = skillctl.main()
        return code, out.getvalue(), err.getvalue()

    def test_description_search_includes_disabled_skills_and_unicode(self):
        for query in ("invoice", "发票", "INVOICE totals"):
            result = skillctl.search_catalog(self.catalog(self.item()), query, 20)
            self.assertEqual(1, result["total"])
            self.assertEqual("off", result["results"][0]["state"])
            self.assertIn("description", result["results"][0]["matched_fields"])

    def test_all_query_words_required_and_aliases_explicit(self):
        catalog = self.catalog(self.item())
        self.assertEqual(0, skillctl.search_catalog(catalog, "invoice unicorn", 20)["total"])
        self.assertEqual(1, skillctl.search_catalog(catalog, "账单", 20, {"账单": ["invoice"]})["total"])

    def test_exact_name_ranks_first_and_limit_reports_total(self):
        catalog = self.catalog(self.item("a"), self.item("invoice", "Other"), self.item("b"))
        result = skillctl.search_catalog(catalog, "invoice", 1)
        self.assertEqual("invoice", result["results"][0]["name"])
        self.assertEqual(3, result["total"])
        self.assertTrue(result["truncated"])
        self.assertEqual(3, len(skillctl.search_catalog(catalog, "invoice", 0)["results"]))

    def test_catalog_failure_does_not_lose_healthy_results_or_leak_parser(self):
        bad = mock.Mock(name="broken")
        bad.name = "codex"
        bad.discover.side_effect = SystemExit("secret parser text")
        healthy = mock.Mock()
        healthy.name = "claude"
        healthy.discover.return_value = [self.item()]
        code, output, _ = self.run_cli(["search", "invoice", "--report", "--json"], [bad, healthy])
        self.assertEqual(1, code)
        self.assertEqual(1, json.loads(output)["total"])
        self.assertNotIn("secret parser", output)

    def test_legacy_json_array_and_diagnostics_on_stderr(self):
        adapter = mock.Mock()
        adapter.discover.return_value = [self.item()]
        code, out, err = self.run_cli(["search", "invoice", "--json"], [adapter])
        self.assertEqual(0, code)
        self.assertIsInstance(json.loads(out), list)
        self.assertEqual(1, json.loads(err)["total"])

    def test_index_roundtrip_search_never_calls_host(self):
        index = self.root / "index.json"
        adapter = mock.Mock()
        adapter.name = "claude"
        adapter.discover.return_value = [self.item()]
        self.run_cli(["index", "--output", str(index), "--json"], [adapter])
        adapter.discover.reset_mock()
        code, out, _ = self.run_cli(["--platform", "claude", "search", "invoice", "--index", str(index), "--report", "--json"], [adapter])
        self.assertEqual(0, code)
        self.assertIn("offline", json.loads(out)["evidence_scope"])
        adapter.discover.assert_not_called()
        original = index.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cli(["index", "--output", str(index)], [adapter])
        self.assertEqual(original, index.read_bytes())

    def test_index_validation(self):
        path = self.root / "bad.json"
        for data in ({}, {**self.catalog(), "errors": ["bad"]}, {**self.catalog(), "skills": [{}]}):
            path.write_text(json.dumps(data))
            with self.assertRaises(SystemExit):
                skillctl.read_index(path)

    def test_empty_query_negative_limit_and_malformed_aliases_rejected(self):
        with self.assertRaises(SystemExit):
            skillctl.search_catalog(self.catalog(), " ", 20)
        with self.assertRaises(SystemExit):
            self.run_cli(["search", "invoice", "--limit", "-1"], [])
        path = self.root / "aliases.json"
        path.write_text('{"invoice": [""]}')
        with self.assertRaises(SystemExit):
            skillctl.load_aliases(path)

    def test_fresh_discovery_sees_new_skill_and_metadata_edits(self):
        adapter = skillctl.ClaudeAdapter(self.cwd)
        path = make_skill(self.home / ".claude" / "skills", "demo")
        first = skillctl.collect_catalog([adapter], {})
        path.write_text('---\nname: demo\ndescription: invoice\n---\nBODY_SENTINEL', encoding="utf-8")
        second = skillctl.collect_catalog([adapter], {})
        self.assertNotEqual(first["skills"][0]["description"], second["skills"][0]["description"])
        self.assertNotIn("BODY_SENTINEL", json.dumps(second))

    def test_codex_plugin_block_stops_entire_batch_before_write(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('[plugins."demo@market"]\nenabled = false\n')
        path = make_skill(config.parent / "plugins" / "cache" / "market" / "demo" / "1" / "skills", "workflow")
        adapter = skillctl.CodexAdapter(self.cwd, config)
        before = config.read_bytes()
        result = adapter.mutate("enable", adapter.discover(), False, False)
        self.assertEqual("plugin_not_enabled_in_selected_config", result["blocked"][0]["reason"])
        self.assertEqual(before, config.read_bytes())
        self.assertTrue(path.exists())
        code, output, _ = self.run_cli(["--platform", "codex", "enable", "workflow", "--json"], [adapter])
        self.assertEqual(1, code)
        self.assertFalse(json.loads(output)["live_verified"])

    def test_inspect_reports_other_layers_without_writes(self):
        path = make_skill(self.home / ".claude" / "skills", "demo")
        local = self.cwd / ".claude" / "settings.local.json"
        local.parent.mkdir()
        local.write_text('{"skillOverrides":{"demo":"off"}}')
        adapter = skillctl.ClaudeAdapter(self.cwd)
        result = skillctl.inspect_targets(adapter, adapter.discover())
        self.assertIn(local.resolve(), [Path(value).resolve() for value in result["additional_config_layers"]])
        self.assertFalse(adapter.config.exists())
        self.assertTrue(path.exists())

    def test_gemini_disable_search_enable_roundtrip_and_backup(self):
        path = make_skill(self.home / ".gemini" / "skills", "demo")
        adapter = skillctl.GeminiAdapter(self.cwd)
        adapter.config.write_text('{"theme":"dark","skills":{"disabled":[]}}')
        original = adapter.config.read_bytes()
        content = path.read_bytes()
        targets = adapter.discover()
        adapter.mutate("disable", targets, True, False)
        self.assertEqual(original, adapter.config.read_bytes())
        result = adapter.mutate("disable", targets, False, False)
        self.assertEqual(original, Path(result["backup"]).read_bytes())
        self.assertEqual("off", adapter.discover()[0].state)
        self.assertEqual(1, skillctl.search_catalog(skillctl.collect_catalog([adapter], {}), "demo", 20)["total"])
        adapter.mutate("enable", adapter.discover(), False, False)
        self.assertEqual("on", adapter.discover()[0].state)
        self.assertEqual("dark", json.loads(adapter.config.read_text())["theme"])
        self.assertEqual(content, path.read_bytes())

    def test_gemini_global_gate_blocks_and_invalid_config_preserved(self):
        make_skill(self.home / ".gemini" / "skills", "demo")
        adapter = skillctl.GeminiAdapter(self.cwd)
        adapter.config.write_text('{"skills":{"enabled":false}}')
        result = adapter.mutate("enable", adapter.discover(), False, False)
        self.assertTrue(result["blocked"])
        for text in ('{"skills":[]}', '{"skills":{"disabled":"demo"}}', '{broken'):
            adapter.config.write_text(text)
            with self.assertRaises(SystemExit):
                adapter.discover()
            self.assertEqual(text, adapter.config.read_text())

    def test_gemini_workspace_scope_does_not_write_user_settings(self):
        make_skill(self.cwd / ".agents" / "skills", "demo")
        adapter = skillctl.GeminiAdapter(self.cwd, scope="workspace")
        adapter.mutate("disable", adapter.discover(), False, False)
        self.assertTrue((self.cwd / ".gemini" / "settings.json").exists())
        self.assertFalse((self.home / ".gemini" / "settings.json").exists())

    def test_opencode_wildcard_order_and_roundtrip_preserves_permissions(self):
        path = make_skill(self.home / ".config" / "opencode" / "skills", "demo")
        adapter = skillctl.OpenCodeAdapter(self.cwd)
        adapter.config.write_text('{"permission":{"bash":"ask","skill":{"demo":"deny","*":"allow"}}}')
        self.assertEqual("on", adapter.discover()[0].state)
        result = adapter.mutate("disable", adapter.discover(), False, False)
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual("off", adapter.discover()[0].state)
        adapter.mutate("enable", adapter.discover(), False, False)
        self.assertEqual("on", adapter.discover()[0].state)
        self.assertEqual("ask", json.loads(adapter.config.read_text())["permission"]["bash"])
        self.assertTrue(path.exists())

    def test_opencode_shorthand_and_jsonc_explicit_selection(self):
        make_skill(self.home / ".config" / "opencode" / "skills", "demo")
        config = self.home / ".config" / "opencode" / "opencode.jsonc"
        config.write_text('{/*comment*/"permission":{"skill":"ask"}}')
        with self.assertRaises(SystemExit):
            skillctl.OpenCodeAdapter(self.cwd).discover()
        adapter = skillctl.OpenCodeAdapter(self.cwd, config)
        self.assertEqual("unknown", adapter.discover()[0].state)
        adapter.mutate("disable", adapter.discover(), False, False)
        rules = json.loads(config.read_text())["permission"]["skill"]
        self.assertEqual({"*": "ask", "demo": "deny"}, rules)

    def test_new_adapters_protect_management_skill(self):
        item = skillctl.Skill("gemini", "manage-agent-skills", "", "test", "test")
        for adapter in (skillctl.GeminiAdapter(self.cwd), skillctl.OpenCodeAdapter(self.cwd)):
            with self.assertRaises(SystemExit):
                adapter.mutate("disable", [item], False, False)
            self.assertFalse(adapter.config.exists())

    def test_opencode_global_default_is_not_reported_as_allow(self):
        make_skill(self.home / ".config" / "opencode" / "skills", "demo")
        adapter = skillctl.OpenCodeAdapter(self.cwd)
        adapter.config.write_text('{"permission":{"*":"deny"}}')
        self.assertEqual("off", adapter.discover()[0].state)
        adapter.mutate("enable", adapter.discover(), False, False)
        rules = json.loads(adapter.config.read_text())["permission"]
        self.assertEqual("deny", rules["*"])
        self.assertEqual({"*": "deny", "demo": "allow"}, rules["skill"])

    def test_custom_home_and_config_paths_are_honored(self):
        custom = self.root / "custom"
        with mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": str(custom), "OPENCODE_CONFIG": str(custom / "oc.json")}):
            path = make_skill(custom / ".gemini" / "skills", "demo")
            adapter = skillctl.GeminiAdapter(self.cwd)
            self.assertEqual(custom / ".gemini" / "settings.json", adapter.config)
            self.assertEqual(str(path.resolve()), adapter.discover()[0].path)
            self.assertEqual(custom / "oc.json", skillctl.OpenCodeAdapter(self.cwd).config)

    def test_selected_host_gates_block_enable_without_rewriting_them(self):
        for adapter, root, content in (
            (skillctl.GeminiAdapter(self.cwd), self.home / ".gemini" / "skills", '{"admin":{"skills":{"enabled":false}}}'),
            (skillctl.OpenCodeAdapter(self.cwd), self.home / ".config" / "opencode" / "skills", '{"tools":{"skill":false}}'),
        ):
            make_skill(root, "demo")
            adapter.config.write_text(content)
            self.assertEqual("off", adapter.discover()[0].state)
            result = adapter.mutate("enable", adapter.discover(), False, False)
            self.assertTrue(result["blocked"])
            self.assertEqual(content, adapter.config.read_text())

    def test_blocked_preset_preflight_never_performs_disable(self):
        preset = self.root / "preset.json"
        preset.write_text(json.dumps({"version": 1, "presets": {"lean": {"codex": {"disable": ["a"], "enable": ["b"]}}}}))
        adapter = mock.Mock()
        adapter.discover.return_value = [skillctl.Skill("codex", name, f"/{name}/SKILL.md", "test", "test") for name in ("a", "b")]
        adapter.mutate.side_effect = [{"changed": True}, {"blocked": [{"reason": "plugin"}]}]
        code, out, _ = self.run_cli(["--platform", "codex", "preset", "lean", "--file", str(preset), "--json"], [adapter])
        self.assertEqual(1, code)
        self.assertTrue(json.loads(out)["blocked"])
        self.assertTrue(all(call.args[2] is True for call in adapter.mutate.call_args_list))


if __name__ == "__main__":
    unittest.main()
