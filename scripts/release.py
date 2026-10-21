#!/usr/bin/env python3
"""Version checks and deterministic artifacts for Manage Agent Skills releases."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "manage-agent-skills"
OWNER = "yunze7373"
SLUG = "manage-agent-skills"
DISPLAY_NAME = "Manage Agent Skills"
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def fail(message: str) -> NoReturn:
    raise SystemExit(f"release error: {message}")


def read_version() -> str:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if not skill_text.startswith("---\n"):
        fail("SKILL.md must start with YAML frontmatter")
    try:
        import yaml
    except ImportError as error:
        fail(f"PyYAML is required to read release metadata: {error}")
    try:
        _, frontmatter, _ = skill_text.split("---", 2)
        document = yaml.safe_load(frontmatter)
    except (ValueError, yaml.YAMLError) as error:
        fail(f"could not read SKILL.md frontmatter: {error}")
    if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict):
        fail("SKILL.md frontmatter must contain a metadata mapping")
    version = document["metadata"].get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        fail("metadata.version in SKILL.md must be a stable x.y.z version")

    tree = ast.parse((SKILL_DIR / "scripts" / "skillctl.py").read_text(encoding="utf-8"))
    cli_version: str | None = None
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in statement.targets
        ):
            cli_version = ast.literal_eval(statement.value)
            break
    if version != cli_version:
        fail(f"SKILL.md version {version} does not match skillctl.py version {cli_version}")
    return version


def verify_tag(tag: str) -> str:
    version = read_version()
    if not tag.startswith("v") or tag[1:] != version:
        fail(f"tag {tag!r} must match the skill version v{version}")
    if not SEMVER.fullmatch(tag[1:]):
        fail(f"tag {tag!r} is not a stable vMAJOR.MINOR.PATCH tag")
    return version


def packaged_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        relative = path.relative_to(SKILL_DIR)
        if (
            "__pycache__" in relative.parts
            or "__MACOSX" in relative.parts
            or path.suffix == ".pyc"
            or any(part.startswith(".") for part in relative.parts)
            or path.name in {".DS_Store", "Thumbs.db"}
        ):
            continue
        if path.is_symlink():
            fail(f"skill package contains a symlink: {relative.as_posix()}")
        if path.is_file():
            files.append((path, f"{SLUG}/{relative.as_posix()}"))
    if not any(name == f"{SLUG}/SKILL.md" for _, name in files):
        fail("skill package is missing SKILL.md")
    files.extend(
        [
            (ROOT / "LICENSE", f"{SLUG}/LICENSE"),
            (ROOT / "requirements.txt", f"{SLUG}/requirements.txt"),
        ]
    )
    return files


def local_skill_hashes() -> dict[str, str]:
    return {
        path.relative_to(SKILL_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path, archive_name in packaged_files()
        if archive_name.startswith(f"{SLUG}/")
        and archive_name not in {f"{SLUG}/LICENSE", f"{SLUG}/requirements.txt"}
    }


def clawhub_scan_exists(version: str) -> bool:
    """Detect submissions that are hidden from public version endpoints.

    ClawHub keeps scan reports for submitted versions even when the version is
    held from public install surfaces. Treat only the exact "version not found"
    response as evidence that a new submission is possible; auth, network, and
    other CLI failures stop the release.
    """
    cli_path = shutil.which("clawhub")
    if not cli_path:
        fail("ClawHub CLI is required to verify hidden submission state")
    with tempfile.TemporaryDirectory(prefix="manage-agent-skills-clawhub-") as temporary:
        report_path = Path(temporary) / "scan.zip"
        try:
            result = subprocess.run(
                [
                    cli_path,
                    "scan",
                    "download",
                    SLUG,
                    "--version",
                    version,
                    "--output",
                    str(report_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            fail(f"could not safely check ClawHub submission state: {error}")

    if result.returncode == 0:
        return True
    if "Skill version not found" in f"{result.stdout}\n{result.stderr}":
        return False
    fail("ClawHub submission state is unknown; refusing to publish or retry this version")


def package(tag: str, output_dir: Path) -> tuple[Path, Path]:
    verify_tag(tag)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{SLUG}-{tag}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path, archive_name in sorted(packaged_files(), key=lambda item: item[1]):
            info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, path.read_bytes())

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksums = output_dir / "SHA256SUMS"
    checksums.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive, checksums


def fetch(
    url: str,
    accept: str = "application/json",
    bearer_token: str | None = None,
) -> tuple[int, bytes | None]:
    headers = {"Accept": accept, "User-Agent": "manage-agent-skills-release"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                delay = error.headers.get("Retry-After", "2") if error.headers else "2"
                try:
                    time.sleep(min(max(float(delay), 1), 30))
                except ValueError:
                    time.sleep(2)
                continue
            if error.code == 404:
                return 404, None
            fail(f"registry returned HTTP {error.code} for {url}")
        except (urllib.error.URLError, TimeoutError) as error:
            fail(f"could not verify registry state: {error}")
    fail("registry rate limit remained after retries")


def fetch_json(url: str, bearer_token: str | None = None) -> tuple[int, Any]:
    status, body = fetch(url, bearer_token=bearer_token)
    if status == 404:
        return status, None
    try:
        return status, json.loads(body or b"")
    except json.JSONDecodeError as error:
        fail(f"registry returned invalid JSON for {url}: {error}")


def check_clawhub(tag: str, *, allow_submitted: bool = False) -> str:
    version = verify_tag(tag)
    base = "https://clawhub.ai/api/v1/skills"
    status, skill_payload = fetch_json(f"{base}/{urllib.parse.quote(SLUG, safe='')}")
    if status == 404:
        return "publish"

    skill = (skill_payload or {}).get("skill") or {}
    owner = (skill_payload or {}).get("owner") or {}
    if skill.get("slug") != SLUG or owner.get("handle", "").casefold() != OWNER.casefold():
        fail(f"ClawHub slug {SLUG!r} is already owned by a different account")

    status, version_payload = fetch_json(f"{base}/{urllib.parse.quote(SLUG, safe='')}/versions/{version}")
    if status == 404:
        if clawhub_scan_exists(version):
            if allow_submitted:
                return "submitted"
            fail(
                f"ClawHub version {version} was already submitted but is not publicly visible; "
                "review its scan/moderation state instead of re-uploading it"
            )
        return "publish"

    published = (version_payload or {}).get("version") or {}
    local_files = local_skill_hashes()
    remote_files = {
        item.get("path"): item.get("sha256")
        for item in published.get("files", [])
        if isinstance(item, dict)
    }
    if skill.get("displayName") != DISPLAY_NAME:
        fail("the existing ClawHub version has a different display name; refusing to treat it as a retry")
    if published.get("version") != version or remote_files != local_files:
        fail("the existing ClawHub version differs from this tag; refusing to overwrite it")
    return "skip"


def verify_clawhub_publication(tag: str, timeout_seconds: int = 1800) -> None:
    """Wait for a new ClawHub submission to appear publicly, then verify bytes."""
    version = verify_tag(tag)
    deadline = time.monotonic() + timeout_seconds
    announced_wait = False
    while True:
        action = check_clawhub(tag, allow_submitted=True)
        if action == "skip":
            print(f"Verified ClawHub version {version} and every packaged file hash.")
            return
        if action == "submitted":
            fail(
                f"ClawHub version {version} has a completed scan report but is not publicly visible; "
                "review its scan/moderation state"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(f"ClawHub version {version} did not become publicly available before timeout")
        if not announced_wait:
            print("Waiting for ClawHub publication checks to finish...", flush=True)
            announced_wait = True
        time.sleep(min(30, remaining))


def check_skillhub(tag: str, namespace: str) -> str:
    version = verify_tag(tag)
    if not namespace.strip():
        fail("SkillHub namespace must not be empty")
    token = os.environ.get("SKILLHUB_TOKEN")
    registry = "https://skill.xfyun.cn"
    coordinate = "/".join(urllib.parse.quote(part, safe="") for part in (namespace, SLUG))
    base = f"{registry}/api/v1/skills/{coordinate}"
    status, skill_payload = fetch_json(base, bearer_token=token)
    if status == 404:
        return "publish"
    skill_data = (skill_payload or {}).get("data") or {}
    if skill_data.get("slug") != SLUG or skill_data.get("namespace") != namespace:
        fail("SkillHub returned a different skill for the requested namespace and slug")

    status, version_payload = fetch_json(
        f"{base}/versions/{urllib.parse.quote(version, safe='')}", bearer_token=token
    )
    if status == 404:
        return "publish"
    version_data = (version_payload or {}).get("data") or {}
    if version_data.get("version") != version:
        fail("SkillHub returned a different version for the requested tag")
    if version_data.get("status") != "PUBLISHED":
        fail(f"SkillHub version {version} is {version_data.get('status', 'unknown')}; resolve its review state before retrying")

    download_url = f"{base}/versions/{urllib.parse.quote(version, safe='')}/download"
    status, archive_bytes = fetch(download_url, accept="application/zip", bearer_token=token)
    if status == 404 or not archive_bytes:
        fail("SkillHub reports a published version but its archive is unavailable")
    if len(archive_bytes) > 100 * 1024 * 1024:
        fail("SkillHub archive exceeded the 100 MiB verification limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            remote_hashes = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()
                if not name.endswith("/")
            }
    except (zipfile.BadZipFile, KeyError, OSError) as error:
        fail(f"could not verify the SkillHub release archive: {error}")
    if remote_hashes != local_skill_hashes():
        fail("the existing SkillHub version differs from this tag; refusing to overwrite it")
    return "skip"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="check tag, skill and CLI versions agree")
    verify_parser.add_argument("tag")

    package_parser = subparsers.add_parser("package", help="create a deterministic release ZIP and checksum")
    package_parser.add_argument("tag")
    package_parser.add_argument("--output-dir", type=Path, required=True)

    clawhub_parser = subparsers.add_parser("clawhub-check", help="fail-closed ClawHub retry check")
    clawhub_parser.add_argument("tag")
    clawhub_parser.add_argument("--github-output", type=Path)

    skillhub_parser = subparsers.add_parser("skillhub-check", help="fail-closed SkillHub retry check")
    skillhub_parser.add_argument("tag")
    skillhub_parser.add_argument("--namespace", required=True)
    skillhub_parser.add_argument("--github-output", type=Path)

    clawhub_verify_parser = subparsers.add_parser(
        "clawhub-verify", help="require the exact ClawHub version to be publicly available"
    )
    clawhub_verify_parser.add_argument("tag")

    args = parser.parse_args()
    if args.command == "verify":
        version = verify_tag(args.tag)
        print(f"Release tag {args.tag} matches skill and CLI version {version}.")
    elif args.command == "package":
        archive, checksums = package(args.tag, args.output_dir)
        print(f"Created {archive} and {checksums}.")
    elif args.command == "clawhub-check":
        action = check_clawhub(args.tag)
        message = f"action={action}\n"
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                output.write(message)
        print(f"ClawHub action: {action}.")
    elif args.command == "clawhub-verify":
        verify_clawhub_publication(args.tag)
    elif args.command == "skillhub-check":
        action = check_skillhub(args.tag, args.namespace)
        message = f"action={action}\n"
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                output.write(message)
        print(f"SkillHub action: {action}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
