#!/usr/bin/env python3
"""Download GitHub Actions ZIP artifacts and extract them only after bounded preflight."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MAX_COMPRESSED = 250 * 1024 * 1024
MAX_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_SINGLE_FILE = 256 * 1024 * 1024
MAX_CENTRAL_DIRECTORY = 64 * 1024 * 1024
MAX_FILES = 20_000
MAX_ARTIFACTS = 50
EOCD_SIGNATURE = b"PK\x05\x06"
EOCD_STRUCT = struct.Struct("<4s4H2LH")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expose the API redirect so credentials are not forwarded cross-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    """Permit signed-URL redirects only while they remain credential-free HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            fail("artifact signed URL redirected to an unsafe location")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fail(message: str) -> None:
    """Terminate with one stable workflow-facing diagnostic."""

    raise SystemExit(message)


def download(artifact_id: int, destination: Path, compressed_so_far: int) -> int:
    """Stream one immutable artifact ZIP with an aggregate compressed-byte ceiling."""

    api_url = os.environ["API_URL"].rstrip("/")
    parsed_api = urllib.parse.urlsplit(api_url)
    if parsed_api.scheme != "https" or not parsed_api.hostname or parsed_api.username or parsed_api.password:
        fail("GitHub API URL is not credential-safe HTTPS")
    repository = urllib.parse.quote(os.environ["REPOSITORY"], safe="/")
    request = urllib.request.Request(
        f"{api_url}/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "User-Agent": "project-toolkit-allure",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        if not location:
            fail("artifact download redirect omitted Location")
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            fail("artifact download returned an unsafe redirect")
        # Never forward the API bearer token to the cross-origin signed URL.
        response = urllib.request.build_opener(HttpsRedirect).open(
            urllib.request.Request(
                location,
                headers={"User-Agent": "project-toolkit-allure"},
            ),
            timeout=30,
        )
    downloaded = 0
    with response, destination.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            downloaded += len(chunk)
            if compressed_so_far + downloaded > MAX_COMPRESSED:
                fail("artifact ZIPs exceed the 250 MiB compressed limit")
            output.write(chunk)
    return downloaded


def preflight_eocd(archive: Path) -> int:
    """Bound central-directory size/count before zipfile allocates member objects."""

    archive_size = archive.stat().st_size
    if archive_size < EOCD_STRUCT.size:
        fail(f"invalid artifact ZIP: {archive.name}")
    read_size = min(archive_size, 65_557)
    with archive.open("rb") as source:
        source.seek(archive_size - read_size)
        trailer = source.read(read_size)
    offset = trailer.rfind(EOCD_SIGNATURE)
    if offset < 0 or offset + EOCD_STRUCT.size > len(trailer):
        fail(f"artifact ZIP has no valid end record: {archive.name}")
    (
        _,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = EOCD_STRUCT.unpack_from(trailer, offset)
    if offset + EOCD_STRUCT.size + comment_size != len(trailer):
        fail(f"artifact ZIP has an inconsistent end record: {archive.name}")
    if disk_number or central_disk or entries_on_disk != total_entries:
        fail(f"multi-disk artifact ZIP rejected: {archive.name}")
    if 0xFFFF in {entries_on_disk, total_entries} or 0xFFFFFFFF in {
        central_size,
        central_offset,
    }:
        fail(f"ZIP64 artifact rejected before extraction: {archive.name}")
    if total_entries > MAX_FILES or central_size > MAX_CENTRAL_DIRECTORY:
        fail(f"artifact ZIP central directory exceeds resource limits: {archive.name}")
    if central_offset + central_size > archive_size - EOCD_STRUCT.size:
        fail(f"artifact ZIP central directory lies outside the archive: {archive.name}")
    counted_entries = 0
    remaining = central_size
    with archive.open("rb") as source:
        source.seek(central_offset)
        while remaining:
            header = source.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                fail(f"artifact ZIP has a malformed central directory: {archive.name}")
            name_size = int.from_bytes(header[28:30], "little")
            extra_size = int.from_bytes(header[30:32], "little")
            member_comment_size = int.from_bytes(header[32:34], "little")
            record_size = 46 + name_size + extra_size + member_comment_size
            if record_size > remaining:
                fail(f"artifact ZIP central record exceeds declared bounds: {archive.name}")
            source.seek(record_size - 46, 1)
            remaining -= record_size
            counted_entries += 1
            if counted_entries > MAX_FILES:
                fail(f"artifact ZIP exceeds the pre-extraction entry limit: {archive.name}")
    if counted_entries != total_entries:
        fail(f"artifact ZIP entry count does not match its end record: {archive.name}")
    return counted_entries


def safe_relative(artifact_name: str, info: zipfile.ZipInfo) -> PurePosixPath | None:
    """Validate one member path and type without extracting it."""

    raw_name = info.filename
    original_name = info.orig_filename
    trimmed_name = raw_name[:-1] if info.is_dir() else raw_name
    parts = trimmed_name.split("/")
    if (
        not trimmed_name
        or raw_name != original_name
        or "\x00" in original_name
        or "\\" in raw_name
        or raw_name.startswith("/")
        or len(raw_name.encode("utf-8")) > 1024
        or any(len(part.encode("utf-8")) > 255 for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_name)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        fail(f"unsafe path in artifact ZIP: {artifact_name}/{raw_name}")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir():
        if file_type not in {0, stat.S_IFDIR}:
            fail(f"non-directory ZIP entry rejected: {artifact_name}/{raw_name}")
        return None
    if file_type not in {0, stat.S_IFREG}:
        fail(f"non-regular ZIP entry rejected: {artifact_name}/{raw_name}")
    if info.flag_bits & 0x1:
        fail(f"encrypted ZIP entry rejected: {artifact_name}/{raw_name}")
    return PurePosixPath(*parts)


def prepare_workspace_directory(destination: Path) -> None:
    """Create a lexical workspace child without following pre-existing symlinks."""

    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink():
        fail("invalid GitHub workspace for validated materialization")
    try:
        relative = destination.relative_to(workspace)
    except ValueError:
        fail("validated materialization target must be inside the workspace")
    if not relative.parts:
        fail("refusing to replace the GitHub workspace")
    current = workspace
    for part in relative.parts[:-1]:
        current /= part
        if os.path.lexists(current):
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                fail(f"unsafe workspace ancestor for materialization: {current}")
    if os.path.lexists(destination):
        metadata = destination.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            fail("unsafe existing workspace materialization target")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)


def main() -> None:
    """Download, preflight, collision-check, and boundedly extract all artifacts."""

    manifest = json.loads(os.environ["ARTIFACT_MANIFEST"])
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= MAX_ARTIFACTS:
        fail("invalid trusted artifact manifest length")
    names: set[str] = set()
    ids: set[int] = set()
    for artifact in manifest:
        if not isinstance(artifact, dict):
            fail("invalid trusted artifact manifest")
        name = artifact.get("name")
        artifact_id = artifact.get("id")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
            or not isinstance(artifact_id, int)
            or isinstance(artifact_id, bool)
            or artifact_id <= 0
            or name in names
            or artifact_id in ids
        ):
            fail("invalid or duplicate trusted artifact manifest entry")
        names.add(name)
        ids.add(artifact_id)

    archive_root = Path(os.environ["ARCHIVE_ROOT"])
    output_root = Path(os.environ["OUTPUT_ROOT"])
    materialize_value = os.environ.get("MATERIALIZE_ROOT")
    materialize_root = Path(materialize_value) if materialize_value else None
    fixture_archives = os.environ.get("ARTIFACT_ARCHIVE_DIR")
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    if not workspace.is_absolute():
        fail("GitHub workspace must be absolute")
    for temporary_root in (archive_root, output_root):
        if not temporary_root.is_absolute():
            fail("artifact archive and extraction roots must be absolute")
        try:
            temporary_root.relative_to(workspace)
        except ValueError:
            pass
        else:
            fail("artifact archives must be downloaded and extracted outside the workspace")
    if archive_root == output_root or archive_root in output_root.parents or output_root in archive_root.parents:
        fail("artifact archive and extraction roots must be disjoint")
    shutil.rmtree(archive_root, ignore_errors=True)
    archive_root.mkdir(parents=True)

    archives: list[tuple[str, Path]] = []
    compressed_total = 0
    for index, artifact in enumerate(manifest, 1):
        name = artifact["name"]
        archive = archive_root / f"artifact-{index}.zip"
        if fixture_archives:
            shutil.copyfile(Path(fixture_archives) / f"{name}.zip", archive)
            compressed_total += archive.stat().st_size
            if compressed_total > MAX_COMPRESSED:
                fail("artifact ZIPs exceed the 250 MiB compressed limit")
        else:
            compressed_total += download(artifact["id"], archive, compressed_total)
        archives.append((name, archive))

    declared_entries = sum(preflight_eocd(archive) for _, archive in archives)
    if declared_entries > MAX_FILES:
        fail("artifact ZIPs exceed the pre-extraction 20,000-entry limit")

    opened: list[tuple[str, zipfile.ZipFile]] = []
    entries: list[tuple[str, zipfile.ZipFile, zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    declared_bytes = 0
    try:
        for artifact_name, archive in archives:
            try:
                bundle = zipfile.ZipFile(archive)
            except (OSError, zipfile.BadZipFile) as error:
                fail(f"invalid artifact ZIP for {artifact_name}: {error}")
            opened.append((artifact_name, bundle))
            for info in bundle.infolist():
                relative = safe_relative(artifact_name, info)
                if relative is None:
                    continue
                if relative in seen:
                    fail(f"duplicate artifact path: {relative}")
                seen.add(relative)
                declared_bytes += info.file_size
                if info.file_size > MAX_SINGLE_FILE:
                    fail(f"artifact entry exceeds 256 MiB: {relative}")
                if len(seen) > MAX_FILES or declared_bytes > MAX_UNCOMPRESSED:
                    fail("artifact ZIPs exceed pre-extraction file-count or 1 GiB limits")
                entries.append((artifact_name, bundle, info, relative))

        if not entries:
            fail("required artifacts contained no regular files")
        shutil.rmtree(output_root, ignore_errors=True)
        output_root.mkdir(parents=True)
        actual_bytes = 0
        for artifact_name, bundle, info, relative in entries:
            destination = output_root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with bundle.open(info) as source, destination.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        actual_bytes += len(chunk)
                        if written > MAX_SINGLE_FILE or actual_bytes > MAX_UNCOMPRESSED:
                            fail("artifact extraction exceeded declared resource limits")
                        output.write(chunk)
            except zipfile.BadZipFile as error:
                fail(f"artifact ZIP failed CRC validation: {artifact_name}/{info.filename}: {error}")
            if written != info.file_size:
                fail(f"artifact entry size mismatch: {artifact_name}/{info.filename}")
        if materialize_root is not None:
            prepare_workspace_directory(materialize_root)
            for _, _, _, relative in entries:
                source = output_root.joinpath(*relative.parts)
                destination = materialize_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
    finally:
        for _, bundle in opened:
            bundle.close()


if __name__ == "__main__":
    main()
