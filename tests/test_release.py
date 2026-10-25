from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile


SCRIPT = Path(__file__).parents[1] / "scripts" / "release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseVersionTests(unittest.TestCase):
    def test_tag_must_match_skill_and_cli_versions(self) -> None:
        self.assertEqual(release.verify_tag("v1.2.0"), "1.2.0")

    def test_tag_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must match the skill version"):
            release.verify_tag("v0.2.2")


class ReleaseArtifactTests(unittest.TestCase):
    def test_package_is_deterministic_and_has_matching_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            archive, sums = release.package("v1.2.0", output)
            first_archive = archive.read_bytes()
            first_sums = sums.read_bytes()

            release.package("v1.2.0", output)

            self.assertEqual(archive.read_bytes(), first_archive)
            self.assertEqual(sums.read_bytes(), first_sums)
            self.assertIn(archive.name, sums.read_text(encoding="ascii"))
            self.assertIn(hashlib.sha256(first_archive).hexdigest(), first_sums.decode("ascii"))
            with ZipFile(archive) as bundle:
                names = bundle.namelist()
            self.assertIn("manage-agent-skills/SKILL.md", names)
            self.assertIn("manage-agent-skills/scripts/skillctl.py", names)


class ClawHubRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = {"SKILL.md": "a" * 64, "scripts/skillctl.py": "b" * 64}
        self.skill = {
            "skill": {"slug": release.SLUG, "displayName": release.DISPLAY_NAME},
            "owner": {"handle": release.OWNER},
        }
        self.version = {
            "version": {
                "version": "1.2.0",
                "files": [
                    {"path": name, "sha256": digest}
                    for name, digest in self.files.items()
                ],
            }
        }

    @mock.patch.object(release, "local_skill_hashes")
    @mock.patch.object(release, "fetch_json")
    def test_exact_published_version_is_skipped(self, fetch_json: mock.Mock, hashes: mock.Mock) -> None:
        hashes.return_value = self.files
        fetch_json.side_effect = [(200, self.skill), (200, self.version)]

        self.assertEqual(release.check_clawhub("v1.2.0"), "skip")

    @mock.patch.object(release, "local_skill_hashes")
    @mock.patch.object(release, "fetch_json")
    def test_conflicting_published_version_stops_release(self, fetch_json: mock.Mock, hashes: mock.Mock) -> None:
        hashes.return_value = self.files
        conflicting = {
            "version": {
                "version": "1.2.0",
                "files": [{"path": "SKILL.md", "sha256": "different"}],
            }
        }
        fetch_json.side_effect = [(200, self.skill), (200, conflicting)]

        with self.assertRaisesRegex(SystemExit, "refusing to overwrite it"):
            release.check_clawhub("v1.2.0")

    @mock.patch.object(release, "clawhub_scan_exists", return_value=False)
    @mock.patch.object(release, "fetch_json")
    def test_publicly_missing_and_unsubmitted_version_is_selected_for_publish(
        self, fetch_json: mock.Mock, scan_exists: mock.Mock
    ) -> None:
        fetch_json.side_effect = [(200, self.skill), (404, None)]

        self.assertEqual(release.check_clawhub("v1.2.0"), "publish")
        scan_exists.assert_called_once_with("1.2.0")

    @mock.patch.object(release, "clawhub_scan_exists", return_value=True)
    @mock.patch.object(release, "fetch_json")
    def test_hidden_submitted_version_is_not_reuploaded(
        self, fetch_json: mock.Mock, scan_exists: mock.Mock
    ) -> None:
        fetch_json.side_effect = [(200, self.skill), (404, None)]

        with self.assertRaisesRegex(SystemExit, "already submitted but is not publicly visible"):
            release.check_clawhub("v1.2.0")

        scan_exists.assert_called_once_with("1.2.0")


class ClawHubScanStateTests(unittest.TestCase):
    @mock.patch.object(release.shutil, "which", return_value="clawhub")
    @mock.patch.object(release.subprocess, "run")
    def test_scan_report_confirms_existing_submission(
        self, run: mock.Mock, which: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "Report ZIP: scan.zip", "")

        self.assertTrue(release.clawhub_scan_exists("1.2.0"))

    @mock.patch.object(release.shutil, "which", return_value="clawhub")
    @mock.patch.object(release.subprocess, "run")
    def test_exact_version_not_found_allows_new_submission(
        self, run: mock.Mock, which: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "Error: Skill version not found")

        self.assertFalse(release.clawhub_scan_exists("1.2.0"))

    @mock.patch.object(release.shutil, "which", return_value="clawhub")
    @mock.patch.object(release.subprocess, "run")
    def test_ambiguous_scan_error_fails_closed(
        self, run: mock.Mock, which: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "Error: unauthorized")

        with self.assertRaisesRegex(SystemExit, "submission state is unknown"):
            release.clawhub_scan_exists("1.2.0")

    @mock.patch.object(release.shutil, "which", return_value=None)
    def test_missing_clawhub_cli_fails_closed(self, which: mock.Mock) -> None:
        with self.assertRaisesRegex(SystemExit, "ClawHub CLI is required"):
            release.clawhub_scan_exists("1.2.0")


class ClawHubPublicationVerificationTests(unittest.TestCase):
    @mock.patch.object(release.time, "sleep")
    @mock.patch.object(release, "check_clawhub", side_effect=["publish", "skip"])
    def test_waits_for_publication_then_verifies(
        self, check: mock.Mock, sleep: mock.Mock
    ) -> None:
        release.verify_clawhub_publication("v1.2.0", timeout_seconds=60)

        self.assertEqual(check.call_count, 2)
        self.assertTrue(check.call_args_list[0].kwargs["allow_submitted"])
        sleep.assert_called_once_with(30)

    @mock.patch.object(release.time, "sleep")
    @mock.patch.object(release, "check_clawhub", side_effect=["submitted", "skip"])
    def test_submitted_scan_report_waits_for_public_verdict(self, check, sleep) -> None:
        release.verify_clawhub_publication("v1.2.0")
        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once()

    @mock.patch.object(release.time, "monotonic", return_value=10)
    @mock.patch.object(release, "check_clawhub", return_value="publish")
    def test_publication_verification_times_out(self, check: mock.Mock, monotonic: mock.Mock) -> None:
        with self.assertRaisesRegex(SystemExit, "did not become publicly available"):
            release.verify_clawhub_publication("v1.2.0", timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()


class NetworkRetryTests(unittest.TestCase):
    @mock.patch.object(release.time, "sleep")
    @mock.patch.object(release.urllib.request, "urlopen")
    def test_transient_read_timeout_retries(self, urlopen, sleep):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b"{}"
        urlopen.side_effect = [TimeoutError("temporary"), response]
        self.assertEqual((200, b"{}"), release.fetch("https://example.invalid"))
        self.assertEqual(2, urlopen.call_count)

    @mock.patch.object(release.time, "sleep")
    @mock.patch.object(release.urllib.request, "urlopen", side_effect=TimeoutError("temporary"))
    def test_persistent_read_timeout_fails_closed(self, urlopen, sleep):
        with self.assertRaisesRegex(SystemExit, "could not verify registry state"):
            release.fetch("https://example.invalid")
        self.assertEqual(3, urlopen.call_count)

    def test_archive_has_platform_independent_creator(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, _ = release.package("v" + release.read_version(), Path(tmp))
            with ZipFile(archive) as bundle:
                self.assertTrue(all(info.create_system == 3 for info in bundle.infolist()))
