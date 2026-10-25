import contextlib
import io
import json
import re
import tempfile
import os
import subprocess
import sys
import yaml
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

from test_skillctl import skillctl
from test_release import release


class AdapterGuideTests(unittest.TestCase):
    def test_every_runtime_platform_has_a_packaged_guide(self):
        payload = skillctl.adapter_guide()
        self.assertEqual(set(skillctl.PLATFORMS), {r["platform"] for r in payload["adapters"]})
        with tempfile.TemporaryDirectory() as tmp:
            archive, _ = release.package("v" + skillctl.VERSION, Path(tmp))
            with ZipFile(archive) as bundle:
                for row in payload["adapters"]:
                    relative = Path(row["guide"]).relative_to(release.SKILL_DIR).as_posix()
                    self.assertIn("manage-agent-skills/" + relative, bundle.namelist())
                self.assertIn("manage-agent-skills/references/add-platform.md", bundle.namelist())

    def test_explicit_target_resolves_without_host_detection(self):
        with mock.patch.object(Path, "home", side_effect=AssertionError("home read")):
            payload = skillctl.adapter_guide("GEMINI")
        self.assertTrue(payload["supported"])
        self.assertFalse(payload["host_detected"])
        self.assertEqual("gemini.md", Path(payload["guide"]).name)

    def test_unknown_and_pathlike_targets_cannot_load_arbitrary_files(self):
        for target in ("new-host", "../../secret", "all", ""):
            payload = skillctl.adapter_guide(target)
            self.assertFalse(payload["supported"])
            self.assertEqual("add-platform.md", Path(payload["guide"]).name)
            self.assertEqual("follow_addition_guide", payload["next_action"])

    def test_cli_guide_does_not_construct_or_discover_adapters(self):
        for args in (["adapter-guide", "--json"], ["adapter-guide", "new-host", "--json"]):
            output = io.StringIO()
            with mock.patch("sys.argv", ["skillctl", *args]), contextlib.redirect_stdout(output), mock.patch.object(skillctl, "adapters", side_effect=AssertionError("host access")):
                self.assertEqual(0, skillctl.main())
            json.loads(output.getvalue())

    def test_missing_reference_fails_instead_of_claiming_support(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(SystemExit, "Incomplete installation"):
                skillctl.adapter_guide("codex")

    def test_unknown_runtime_platform_does_not_fall_through_to_hermes(self):
        with mock.patch.object(skillctl, "HermesAdapter", side_effect=AssertionError("wrong host")):
            with self.assertRaisesRegex(SystemExit, "Unsupported platform"):
                skillctl.adapters(SimpleNamespace(platform="new-host"))

    def test_all_relative_document_links_resolve_inside_skill(self):
        root = release.SKILL_DIR.resolve()
        for document in root.rglob("*.md"):
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                if "://" in link or link.startswith("#"):
                    continue
                target = (document.parent / link.split("#")[0]).resolve()
                self.assertTrue(target.is_relative_to(root), (document, link))
                self.assertTrue(target.is_file(), (document, link))


class ExtractedAdapterIntegrationTests(unittest.TestCase):
    def test_extracted_zip_and_new_readonly_adapter_through_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive, _ = release.package("v" + skillctl.VERSION, root / "dist")
            with ZipFile(archive) as bundle:
                bundle.extractall(root / "extracted")
            package = root / "extracted/manage-agent-skills"
            script = package / "scripts/skillctl.py"
            home = root / "home"
            home.mkdir()
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(("CODEX", "CLAUDE", "COPILOT", "HERMES", "OPENCLAW", "GEMINI", "OPENCODE", "XDG_")):
                    env.pop(key)
            env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home), LOCALAPPDATA=str(home),
                       PYTHONPATH=str(Path(yaml.__file__).resolve().parent.parent), PYTHONIOENCODING="utf-8")
            def run(*args, code=0):
                result = subprocess.run([sys.executable, str(script), "--cwd", str(project), *args],
                                        cwd=project, env=env, capture_output=True, text=True,
                                        encoding="utf-8", timeout=30)
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                return json.loads(result.stdout)
            for name in skillctl.PLATFORMS:
                payload = run("adapter-guide", name, "--json")
                self.assertTrue(Path(payload["guide"]).is_relative_to(package))
                self.assertTrue(Path(payload["guide"]).is_file())
            self.assertFalse(run("adapter-guide", "example", "--json")["supported"])
            # Implement the guide's integration checklist in this disposable package.
            source = script.read_text(encoding="utf-8")
            source = source.replace('PLATFORMS = (', 'PLATFORMS = ("example", ', 1)
            position = source.index('def adapters(')
            implementation = """class ExampleAdapter:
    name = "example"
    def __init__(self, cwd):
        self.cwd = cwd
    def available(self):
        return True, "synthetic read-only host"
    def discover(self):
        return scan_roots(self.name, [(self.cwd / "skills", "example", "fixture", SKILL_GLOB_FLAT)])
    def mutate(self, action, targets, dry_run, force):
        return {"platform": self.name, "changed": False, "dry_run": dry_run,
                "blocked": [{"reason": "unsupported_operation"}]}


"""
            source = source[:position] + implementation + source[position:]
            source = source.replace('        if name == "codex":',
                                    '        if name == "example":\n            result.append(ExampleAdapter(args.cwd))\n        elif name == "codex":', 1)
            source = source.replace('REFRESH_GUIDANCE = {', 'REFRESH_GUIDANCE = {\n    "example": "Read-only fixture; no refresh needed.",', 1)
            script.write_text(source, encoding="utf-8")
            (package / "references/adapters/example.md").write_text("# Synthetic host\nRead-only fixture.")
            fixture = project / "skills/demo/SKILL.md"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("---\nname: demo\ndescription: Deployment helper\n---\n")
            before = {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}
            self.assertTrue(run("adapter-guide", "example", "--json")["supported"])
            base = ("--platform", "example")
            self.assertEqual(1, run(*base, "status", "--json")["platforms"][0]["discovered"])
            self.assertEqual(1, len(run(*base, "audit", "--json")["skills"]))
            self.assertEqual(1, len(run(*base, "doctor", "--json")["diagnostic_skills"]))
            self.assertEqual(1, run(*base, "search", "Deployment", "--report", "--json")["total"])
            self.assertTrue(run(*base, "inspect", "demo", "--json", code=1)["enable_preview"]["blocked"])
            for action in ("enable", "disable"):
                for flags in ([], ["--dry-run"]):
                    self.assertFalse(run(*base, action, "demo", *flags, "--json", code=1)["changed"])
            index = root / "index.json"
            run(*base, "index", "--output", str(index), "--json")
            self.assertEqual(1, run(*base, "search", "demo", "--index", str(index), "--report", "--json")["total"])
            preset = root / "presets.json"
            preset.write_text(json.dumps({"version": 1, "presets": {"test": {"example": {"disable": ["demo"]}}}}))
            self.assertTrue(run(*base, "preset", "test", "--file", str(preset), "--json", code=1)["blocked"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()})
            self.assertEqual([], list(home.rglob("*")))

