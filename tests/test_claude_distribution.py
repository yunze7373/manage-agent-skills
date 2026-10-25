import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from test_skillctl import skillctl, make_skill
from test_release import release


class ClaudeDistributionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / ".git").mkdir()
        patch = mock.patch.object(Path, "home", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_snapshots_reported_readonly_with_version_and_legacy(self):
        normal = make_skill(self.root / ".claude/skills", "manage-agent-skills")
        paths = [normal]
        for n in range(4):
            paths.append(make_skill(self.root / f".claude/skills/synced/{n}", "manage-agent-skills"))
        desktop = self.root / "Claude-Data/skills-plugin"
        paths.append(make_skill(desktop / "account/session/skills", "manage-agent-skills"))
        legacy = make_skill(self.root / ".claude/skills", "manage-codex-skills")
        for path in paths[:3]:
            path.write_text('---\nname: manage-agent-skills\ndescription: test\nmetadata:\n  version: "1.0.0"\n---\n')
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        adapter = skillctl.ClaudeAdapter(self.root, self.root / "settings.json", [desktop, desktop])
        report = skillctl.audit_payload([adapter])
        codes = {f["code"] for f in report["findings"]}
        self.assertTrue({"duplicate_identity", "outdated_manager", "manager_version_unknown",
                         "incomplete_manager", "readonly_snapshot", "replaceable_legacy_manager"} <= codes)
        self.assertEqual(7, len(report["skills"]))
        self.assertEqual({str(normal), str(legacy)}, {i.path for i in adapter.discover()})
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_doctor_cli_reports_executing_version_and_snapshot(self):
        make_skill(self.root / ".claude/skills/synced/account", "manage-agent-skills")
        out = io.StringIO()
        argv = ["skillctl", "--platform", "claude", "--cwd", str(self.root),
                "--claude-config", str(self.root / "settings.json"), "doctor", "--json"]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
            self.assertEqual(0, skillctl.main())
        report = json.loads(out.getvalue())
        self.assertEqual(skillctl.VERSION, report["version"])
        self.assertEqual(1, len(report["diagnostic_skills"]))

    def test_doctor_text_includes_version_and_diagnostics(self):
        make_skill(self.root / ".claude/skills/synced/account", "manage-agent-skills")
        out = io.StringIO()
        with mock.patch("sys.argv", ["skillctl", "--platform", "claude", "--cwd", str(self.root), "doctor"]), contextlib.redirect_stdout(out):
            self.assertEqual(0, skillctl.main())
        self.assertIn("version=" + skillctl.VERSION, out.getvalue())
        self.assertIn("incomplete_manager", out.getvalue())

    def test_current_and_newer_versions_are_not_outdated(self):
        for version in (skillctl.VERSION, "99.0.0"):
            path = self.root / "SKILL.md"
            path.write_text(f'---\nname: manage-agent-skills\ndescription: test\nmetadata:\n  version: "{version}"\n---\n')
            adapter = mock.Mock()
            adapter.name = "fixture"
            adapter.discover.return_value = [skillctl.Skill("fixture", "manage-agent-skills", str(path), "test", "test", "on")]
            self.assertNotIn("outdated_manager", {f["code"] for f in skillctl.audit_payload([adapter])["findings"]})

    def test_entrypoint_stops_before_workflow_and_supplies_repair(self):
        source = release.SKILL_DIR / "SKILL.md"
        # Simulate a SKILL.md-only upload; contract test, not a live model invocation.
        target = self.root / "SKILL.md"
        target.write_bytes(source.read_bytes())
        self.assertFalse((self.root / "scripts/skillctl.py").exists())
        text = target.read_text(encoding="utf-8")
        self.assertLess(text.index("First: installation self-check"), text.index("Choose the workflow"))
        for required in ("STOP", "安装不完整，请重新上传完整 zip", "Do not guess", skillctl.VERSION,
                         f"manage-agent-skills-v{skillctl.VERSION}.zip"):
            self.assertIn(required, text)
        readme = (release.ROOT / "README.md").read_text(encoding="utf-8")
        for required in ("上传到 Claude（claude.ai / Desktop）", "不能只传 SKILL.md", "anthropic-skills",
                         "从 manage-codex-skills 迁移", "不是逐命令无差异替换"):
            self.assertIn(required, readme)


class ClaudeArchiveTests(unittest.TestCase):
    def test_invalid_archives_fail_closed(self):
        valid = {"manage-agent-skills/SKILL.md": "---\nname: manage-agent-skills\ndescription: test\n---\n",
                 "manage-agent-skills/scripts/skillctl.py": "print(1)",
                 "manage-agent-skills/references/platforms.md": "reference"}
        variants = []
        for key in valid:
            variants.append({k: v for k, v in valid.items() if k != key})
        for bad in ("---\nname: [\n---\n", "---\n- list\n---\n", "name: missing fence",
                    "---\nname: manage-agent-skills\n---\n",
                    "---\nname: manage-agent-skills\ndescription: " + "x" * 201 + "\n---\n"):
            variants.append({**valid, "manage-agent-skills/SKILL.md": bad})
        for bad_path in ("SKILL.md", "other/file", "manage-agent-skills/../escape"):
            variants.append({**valid, bad_path: "bad"})
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "test.zip"
            for files in variants:
                with self.subTest(files=list(files)):
                    with ZipFile(archive, "w") as bundle:
                        for key, value in files.items():
                            bundle.writestr(key, value)
                    with self.assertRaises(SystemExit):
                        release.validate_claude_zip(archive)
            with ZipFile(archive, "w") as bundle:
                for key, value in valid.items():
                    bundle.writestr(key, value)
            release.validate_claude_zip(archive)

    def test_packaging_runs_validator_before_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(release, "validate_claude_zip", side_effect=SystemExit("invalid")) as validate:
                with self.assertRaises(SystemExit):
                    release.package("v" + skillctl.VERSION, Path(tmp))
            validate.assert_called_once()
            self.assertFalse((Path(tmp) / "SHA256SUMS").exists())
