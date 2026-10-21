"""Publish verification and artifact preparation for skillhub.cn (not xfyun)."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import time
import urllib.parse
import zipfile
from pathlib import Path

import yaml

from release import DISPLAY_NAME, SLUG, fail, fetch, fetch_json, verify_tag

API = "https://api.skillhub.cn"
NAMESPACE = "indiv-xmemo"
OWNER = "user_aad66cab"


def prepare(tag: str, archive: Path, checksums: Path, destination: Path) -> None:
    version = verify_tag(tag)
    expected_name = f"{SLUG}-{tag}.zip"
    expected = checksums.read_text(encoding="ascii").split()
    if len(expected) != 2 or expected[1] != expected_name or archive.name != expected_name:
        fail("release checksum manifest does not identify the requested archive")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected[0]:
        fail("GitHub release archive checksum mismatch")
    if destination.exists():
        fail("payload destination already exists")
    with zipfile.ZipFile(archive) as bundle:
        payload = {}
        for info in bundle.infolist():
            parts = info.filename.split("/")
            if parts[0] != SLUG or len(parts) < 2 or any(p in ("", ".", "..") for p in parts) or "\\" in info.filename:
                fail("unsafe release archive path")
            name = "/".join(parts[1:])
            if name in payload or info.file_size > 10 * 1024 * 1024 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                fail("invalid release archive member")
            # SkillHub rejects extensionless filenames; MIT is declared in the
            # SkillHub frontmatter below, so omit the repository LICENSE file.
            if not Path(name).suffix:
                if name == "LICENSE":
                    continue
                fail(f"SkillHub payload contains an unsupported extensionless file: {name}")
            payload[name] = bundle.read(info)
    text = payload["SKILL.md"].decode("utf-8")
    _, front, body = text.split("---", 2)
    metadata = yaml.safe_load(front)
    if metadata.get("metadata", {}).get("version") != version:
        fail("release archive skill version mismatch")
    additions = {"displayName": DISPLAY_NAME, "slug": SLUG, "version": version,
                 "summary": metadata["description"], "license": "MIT",
                 "homepage": "https://github.com/yunze7373/manage-agent-skills"}
    if set(metadata) & set(additions):
        fail("platform frontmatter fields already exist")
    extra = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in additions.items())
    payload["SKILL.md"] = ("---\n" + front.strip() + "\n" + extra + "\n---" + body).encode("utf-8")
    if any(b"\r" in data for data in payload.values()):
        fail("release payload contains CR bytes")
    for name, data in payload.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"Prepared {len(payload)} release files; only SKILL.md metadata adapted for skillhub.cn")


def details() -> dict:
    query = urllib.parse.urlencode({"coordinate": f"@{NAMESPACE}/{SLUG}"})
    status, data = fetch_json(f"{API}/api/v1/skills/resolve?{query}")
    if status != 200 or not isinstance(data, dict):
        fail("existing SkillHub target could not be resolved")
    if (data.get("namespace", {}).get("canonicalName") != f"@{NAMESPACE}/{SLUG}"
            or data.get("owner", {}).get("handle") != OWNER):
        fail("SkillHub target namespace or owner mismatch")
    return data


def check(tag: str, payload: Path, *, authenticate: bool = True) -> str:
    version = verify_tag(tag)
    data = details()
    if authenticate:
        key = os.environ.get("SKILLHUB_KEY", "")
        if not key:
            fail("SKILLHUB_KEY is required")
        status, identity = fetch_json(f"{API}/api/v1/auth/me", bearer_token=key)
        if status != 200 or (identity.get("user") or identity).get("handle") != OWNER:
            fail("SkillHub credential does not own the existing indiv-xmemo listing")
    latest = data.get("latestVersion", {}).get("version", "")
    try:
        if tuple(map(int, version.split("."))) < tuple(map(int, latest.split("."))):
            fail(f"SkillHub latest is {latest}; release {version} would be a downgrade")
    except ValueError:
        fail("SkillHub returned an unsupported latest version")
    query = urllib.parse.urlencode({"slug": SLUG, "namespace": NAMESPACE, "version": version})
    status, content = fetch(f"{API}/api/v1/download?{query}", accept="application/zip")
    if status == 404:
        return "publish"
    try:
        with zipfile.ZipFile(io.BytesIO(content or b"")) as bundle:
            names = [i.filename for i in bundle.infolist() if not i.is_dir()]
            if len(names) != len(set(names)) or sum(i.file_size for i in bundle.infolist()) > 100 * 1024 * 1024:
                fail("invalid SkillHub archive")
            remote = {name: hashlib.sha256(bundle.read(name)).hexdigest() for name in names}
            # skillhub.cn download bundles can include a registry metadata sidecar.
            if "_meta.json" in remote and not (payload / "_meta.json").exists():
                sidecar = json.loads(bundle.read("_meta.json"))
                if sidecar.get("slug") != SLUG or sidecar.get("version") != version:
                    fail("SkillHub archive metadata identifies a different release")
                del remote["_meta.json"]
    except zipfile.BadZipFile:
        fail("SkillHub did not return a valid public archive")
    local = {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in payload.rglob("*") if p.is_file()}
    if "SKILL.md" not in local or remote != local:
        fail("SkillHub archive differs from the requested release; refusing duplicate publication")
    if latest != version:
        fail("SkillHub exact version exists but is not latest")
    return "skip"


def verify(tag: str, payload: Path, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    while check(tag, payload, authenticate=False) != "skip":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("SkillHub exact release did not become publicly available")
        time.sleep(min(30, remaining))
    print("Verified SkillHub namespace, latest version and every public archive file hash")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "check", "verify"])
    parser.add_argument("tag")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.archive or not args.checksums:
            parser.error("prepare requires --archive and --checksums")
        prepare(args.tag, args.archive, args.checksums, args.payload)
    elif args.command == "verify":
        verify(args.tag, args.payload)
    else:
        action = check(args.tag, args.payload)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as out:
                out.write(f"action={action}\n")
        print(f"SkillHub action: {action}")


if __name__ == "__main__":
    main()
