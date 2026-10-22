import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import skillhub_release as hub
import release


class SkillHubTests(unittest.TestCase):
    def setUp(self):
        self.details = {"namespace": {"canonicalName": "@indiv-xmemo/manage-agent-skills"},
                        "owner": {"handle": hub.OWNER}, "latestVersion": {"version": "1.0.0"},
                        "skill": {"tags": {"latest": "1.0.0"}}}

    def test_prepared_payload_changes_only_skill_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksum = release.package("v1.1.1", root)
            hub.prepare("v1.1.1", archive, checksum, root / "payload")
            with ZipFile(archive) as bundle:
                changed = []
                omitted = []
                for name in bundle.namelist():
                    relative = name.split("/", 1)[1]
                    target = root / "payload" / relative
                    if not target.exists():
                        omitted.append(relative)
                        continue
                    new = target.read_bytes()
                    if new != bundle.read(name):
                        changed.append(relative)
                self.assertEqual(changed, ["SKILL.md"])
                self.assertEqual(omitted, ["LICENSE"])
                old_body = bundle.read("manage-agent-skills/SKILL.md").split(b"---", 2)[2]
            new_text = (root / "payload/SKILL.md").read_bytes()
            self.assertEqual(new_text.split(b"---", 2)[2], old_body)
            self.assertIn(b'version: "1.1.1"', new_text)

    def test_bad_checksum_rejects_before_writing_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksum = release.package("v1.1.1", root)
            archive.write_bytes(b"tampered")
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                hub.prepare("v1.1.1", archive, checksum, root / "payload")
            self.assertFalse((root / "payload").exists())

    def test_other_extensionless_files_rejected_before_writing_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksum = release.package("v1.1.1", root)
            with ZipFile(archive, "a") as bundle:
                bundle.writestr("manage-agent-skills/NOTICE", b"unsupported extensionless file")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
            with self.assertRaisesRegex(SystemExit, "unsupported extensionless file: NOTICE"):
                hub.prepare("v1.1.1", archive, checksum, root / "payload")
            self.assertFalse((root / "payload").exists())

    @mock.patch.object(hub, "fetch_json")
    def test_wrong_namespace_rejected(self, fetch):
        self.details["namespace"]["canonicalName"] = "@other/manage-agent-skills"
        fetch.return_value = (200, self.details)
        with self.assertRaisesRegex(SystemExit, "namespace or owner mismatch"):
            hub.details()

    @mock.patch.object(hub, "details")
    @mock.patch.object(hub, "fetch_json")
    @mock.patch.dict(hub.os.environ, {"SKILLHUB_KEY": "synthetic-test-key"})
    def test_wrong_credential_owner_rejected(self, fetch, details):
        details.return_value = self.details
        fetch.return_value = (200, {"user": {"handle": "wrong-owner"}})
        with self.assertRaisesRegex(SystemExit, "does not own"):
            hub.check("v1.1.1", Path("unused"))

    @mock.patch.object(hub, "details")
    def test_downgrade_rejected(self, details):
        self.details["latestVersion"]["version"] = "1.1.2"
        details.return_value = self.details
        with self.assertRaisesRegex(SystemExit, "downgrade"):
            hub.check("v1.1.1", Path("unused"), authenticate=False)

    @mock.patch.object(hub, "details")
    @mock.patch.object(hub, "fetch")
    def test_exact_archive_required_for_skip(self, fetch, details):
        self.details["latestVersion"]["version"] = "1.1.1"
        self.details["skill"]["tags"]["latest"] = "1.1.1"
        details.return_value = self.details
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_bytes(b"skill")
            out = io.BytesIO()
            with ZipFile(out, "w") as bundle:
                bundle.writestr("SKILL.md", b"skill")
            fetch.return_value = (200, out.getvalue())
            self.assertEqual(hub.check("v1.1.1", root, authenticate=False), "skip")
            (root / "SKILL.md").write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "differs"):
                hub.check("v1.1.1", root, authenticate=False)

    @mock.patch.object(hub, "details")
    @mock.patch.object(hub, "fetch")
    def test_registry_sidecar_must_match_exact_version(self, fetch, details):
        import json
        self.details["latestVersion"]["version"] = "1.1.1"
        self.details["skill"]["tags"]["latest"] = "1.1.1"
        details.return_value = self.details
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_bytes(b"skill")
            for version, valid in [("1.1.1", True), ("0.1.5", False)]:
                out = io.BytesIO()
                with ZipFile(out, "w") as bundle:
                    bundle.writestr("SKILL.md", b"skill")
                    bundle.writestr("_meta.json", json.dumps({"slug": hub.SLUG, "version": version}))
                fetch.return_value = (200, out.getvalue())
                if valid:
                    self.assertEqual(hub.check("v1.1.1", root, authenticate=False), "skip")
                else:
                    with self.assertRaisesRegex(SystemExit, "different release"):
                        hub.check("v1.1.1", root, authenticate=False)

    @mock.patch.object(hub, "details")
    @mock.patch.object(hub, "fetch")
    def test_submitted_review_version_is_not_republished(self, fetch, details):
        self.details["skill"]["tags"]["latest"] = "1.1.1"
        details.return_value = self.details
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_bytes(b"skill")
            out = io.BytesIO()
            with ZipFile(out, "w") as bundle:
                bundle.writestr("SKILL.md", b"skill")
            fetch.return_value = (200, out.getvalue())
            self.assertEqual(hub.check("v1.1.1", root, authenticate=False), "submitted")

    @mock.patch.object(hub, "details")
    @mock.patch.object(hub, "check", return_value="submitted")
    def test_verification_reports_pending_review_without_retry(self, check, details):
        details.return_value = self.details
        hub.verify("v1.1.1", Path("unused"))
        check.assert_called_once_with("v1.1.1", Path("unused"), authenticate=False)

    @mock.patch.object(hub.time, "monotonic", return_value=10)
    @mock.patch.object(hub, "check", return_value="publish")
    def test_missing_public_version_cannot_pass_verification(self, check, clock):
        with self.assertRaisesRegex(SystemExit, "did not become publicly available"):
            hub.verify("v1.1.1", Path("unused"), timeout=0)

    @mock.patch.object(hub.time, "sleep")
    @mock.patch.object(hub, "check", side_effect=["publish", "skip"])
    def test_verification_waits_for_public_archive(self, check, sleep):
        hub.verify("v1.1.1", Path("unused"))
        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once()
