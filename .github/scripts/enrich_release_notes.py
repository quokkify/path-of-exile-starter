"""Deterministically enrich Release Please output from selected PR bodies."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

RICH_HEADINGS = {
    "release notes": "Release notes",
    "highlight": "Highlights",
    "usage example": "Usage Examples",
    "migration": "Migration",
    "breaking change": "Breaking Changes",
    "dependencies": "📦 Dependencies",
}
MARKER = "<!-- project-toolkit:rich-release-notes pr={number} -->"
MARKER_PREFIX = "<!-- project-toolkit:rich-release-notes "
MARKER_PATTERN = re.compile(r"<!-- project-toolkit:rich-release-notes pr=([0-9]+) -->")
STANDALONE_MARKER_PATTERN = re.compile(
    r"^\s*<!-- project-toolkit:rich-release-notes pr=([0-9]+) -->\s*$"
)
INLINE_MARKER_PATTERN = re.compile(
    r"^\s*[-*+]\s+.+?\s+<!-- project-toolkit:rich-release-notes pr=([0-9]+) -->\s*$"
)
BLOCK_START = "<!-- project-toolkit:rich-block:start -->"
BLOCK_END = "<!-- project-toolkit:rich-block:end -->"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SAFE_PATH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
DEPENDENCY_TITLE_PATTERN = re.compile(r"^(?:chore|deps)\(deps\):\s+", re.IGNORECASE)
BARE_CHORE_TITLE_PATTERN = re.compile(r"^chore:\s+", re.IGNORECASE)
DEPENDENCIES_HEADING = "### 📦 Dependencies"


class EnrichmentError(RuntimeError):
    """Raised when release metadata is unsafe or ambiguous."""


def _render_dependency_title(title: str, *, number: str, pr: Mapping[str, object]) -> str:
    """Render a title-only dependency with visible source attribution."""
    description = DEPENDENCY_TITLE_PATTERN.sub("", title, count=1).strip() or "Dependency update"
    pr_url = pr.get("pr_url")
    attribution = f"([#{number}]({pr_url}))" if isinstance(pr_url, str) and pr_url else f"(#{number})"
    commit_url = pr.get("commit_url")
    commit_sha = pr.get("commit_sha")
    if isinstance(commit_url, str) and commit_url and isinstance(commit_sha, str) and commit_sha:
        attribution += f" ([{commit_sha[:7]}]({commit_url}))"
    return f"- {description} {attribution}"


def _render_dependency_content(content: str) -> str:
    """Normalize dependency entries to bullets without losing Markdown links."""
    rendered: list[str] = []
    for line in content.splitlines():
        line = DEPENDENCY_TITLE_PATTERN.sub("", line.strip(), count=1).strip()
        if not line:
            continue
        if not re.match(r"^(?:[-*+] |\d+[.)] )", line):
            line = f"- {line}"
        rendered.append(line)
    return "\n".join(rendered) or "- Dependency update"


def _add_dependency_marker(content: str, marker: str) -> str:
    """Keep the source marker inside the dependency list item."""
    lines = content.splitlines()
    lines[0] = f"{lines[0]} {marker}"
    return "\n".join(lines)


def _without_comments(lines: Iterable[str]) -> str:
    text = "\n".join(lines).strip()
    return re.sub(r"<!--[\s\S]*?-->", "", text).strip()


def extract_rich_sections(body: str) -> dict[str, str]:
    """Extract supported level-2 sections, retaining Markdown and fences."""
    result: dict[str, str] = {}
    current: str | None = None
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fence: tuple[str, int] | None = None
    for line in lines:
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            if current is not None:
                result[current] += line + "\n"
            continue
        if fence is not None:
            if current is not None:
                result[current] += line + "\n"
            continue
        match = re.match(r"^##[ \t]+([^#].*?)[ \t]*$", line)
        if match:
            heading = match.group(1).strip().casefold()
            current = heading if heading in RICH_HEADINGS and heading not in result else None
            if current is not None:
                result[current] = ""
            continue
        if current is not None:
            result[current] += line + "\n"
    return {key: value.strip() for key, value in result.items() if _without_comments(value.splitlines())}


def _version_ranges(changelog: str) -> list[tuple[int, int]]:
    lines = changelog.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in lines:
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
        elif fence is None and re.match(r"^##[ \t]+.*$", line):
            offsets.append(offset)
        offset += len(line)
    matches = offsets
    return [(start, matches[i + 1] if i + 1 < len(matches) else len(changelog)) for i, start in enumerate(matches)]


def _rich_numbers(text: str) -> set[str]:
    """Read canonical markers outside fences, not arbitrary prose."""
    numbers: set[str] = set()
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            token = fence_match.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is None:
            marker = None
            if line.count(MARKER_PREFIX) == 1:
                marker = STANDALONE_MARKER_PATTERN.fullmatch(line) or INLINE_MARKER_PATTERN.fullmatch(line)
            if marker:
                numbers.add(marker.group(1))
    return numbers


def _remove_legacy_block(top: str) -> str:
    """Remove marker-only notes without treating fenced headings as boundaries."""
    output: list[str] = []
    removing = False
    fence: tuple[str, int] | None = None
    for line in top.splitlines(keepends=True):
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            token = fence_match.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            if not removing:
                output.append(line)
            continue
        if fence is None and re.match(r"^<!-- project-toolkit:rich-release-notes pr=\d+ -->\s*$", line):
            removing = True
            continue
        if removing and fence is None:
            heading = re.match(r"^###\s+(.+?)\s*$", line)
            if heading and heading.group(1).strip().casefold() not in {value.casefold() for value in RICH_HEADINGS.values()}:
                removing = False
            elif re.match(r"^##[ \t]+", line):
                removing = False
        if not removing:
            output.append(line)
    return "".join(output)


def _render_entries(prs: Iterable[Mapping[str, object]], excluded: set[str]) -> str:
    entries: list[tuple[int, str, dict[str, str], bool, Mapping[str, object]]] = []
    seen: set[str] = set()
    for pr in prs:
        number = str(pr.get("number", "")).strip()
        if not number.isdigit() or number in excluded or number in seen:
            continue
        seen.add(number)
        sections = extract_rich_sections(str(pr.get("body", "")))
        if not sections and pr.get("legacy_dependency") is True:
            title = str(pr.get("title", "")).strip()
            if title:
                sections = {"dependencies": title}
        # PR bodies are untrusted; reserved delimiters must not be able to
        # terminate or forge the machine-owned block on a later rerun.
        title = str(pr.get("title", "")).strip()
        reserved = (
            BLOCK_START.casefold(),
            BLOCK_END.casefold(),
            "project-toolkit:rich-release-notes",
            "<details",
            "</details",
            "<summary",
            "</summary",
        )
        untrusted = [title.casefold(), *(value.casefold() for value in sections.values())]
        is_dependency = bool(DEPENDENCY_TITLE_PATTERN.match(title))
        # Native dependency PRs do not have a rich section in their body, but
        # their title is still useful context in the enriched block.  Ordinary
        # bare chore PRs, on the other hand, must not create a synthetic note.
        if not sections and BARE_CHORE_TITLE_PATTERN.match(title):
            continue
        if (sections or is_dependency) and not any(
            marker in value
            for value in untrusted
            for marker in reserved
        ):
            entries.append((int(number), title, sections, pr.get("legacy_dependency") is True, pr))
    entries.sort(key=lambda item: item[0])
    # Keep every dependency bullet together. Markdown treats a heading,
    # paragraph, or another list as a boundary, so rendering in PR-number
    # order would split the dependency list when a rich non-dependency PR is
    # interleaved between dependency PRs. A mixed entry must also defer its
    # non-dependency sections until this first phase is complete.
    entries.sort(key=lambda item: item[0])
    blocks: list[str] = []
    dependency_heading_written = False
    for number_value, title, sections, legacy_dependency, pr in entries:
        number = str(number_value)
        has_dependency_section = "dependencies" in sections
        if not has_dependency_section:
            continue
        if not dependency_heading_written:
            blocks.append(DEPENDENCIES_HEADING)
            dependency_heading_written = True
        content = sections["dependencies"]
        if legacy_dependency and content == title:
            content = _render_dependency_title(title, number=number, pr=pr)
        else:
            content = _render_dependency_content(content)
        blocks.append(_add_dependency_marker(content, MARKER.format(number=number)))
    for number_value, title, sections, legacy_dependency, _pr in entries:
        number = str(number_value)
        has_dependency_section = "dependencies" in sections
        if not has_dependency_section:
            blocks.append(MARKER.format(number=number))
            if title and not legacy_dependency:
                blocks.append(f"#### {title}")
        for key, heading in RICH_HEADINGS.items():
            if key == "dependencies":
                continue
            if key in sections:
                content = re.sub(
                    rf"^\s*{re.escape(DEPENDENCIES_HEADING)}\s*$\n?",
                    "",
                    sections[key],
                    flags=re.MULTILINE,
                ).strip()
                blocks.extend((f"### {heading}", content, ""))
    return "\n".join(blocks).rstrip()


LEGACY_DEPENDENCY_COMMIT = re.compile(
    r"^chore\(deps\):.*?\(#(?P<number>[0-9]+)\)\s*$"
)
VERSION_HEADING = re.compile(
    r"^##[ \t]+(?:\[(?P<linked>[0-9]+\.[0-9]+\.[0-9]+)\]\([^)]*\)|"
    r"(?P<plain>[0-9]+\.[0-9]+\.[0-9]+))"
)


def legacy_dependency_pr_numbers(changelog: str) -> list[int]:
    """Find pre-native dependency PRs since the previous generated release."""
    versions = []
    for line in changelog.splitlines():
        match = VERSION_HEADING.match(line)
        if match:
            versions.append(match.group("linked") or match.group("plain"))
    if len(versions) < 2:
        return []
    completed = _run_git(["log", f"v{versions[1]}..HEAD", "--format=%s"])
    numbers = {
        int(match.group("number"))
        for line in completed.stdout.splitlines()
        if (match := LEGACY_DEPENDENCY_COMMIT.fullmatch(line.strip()))
    }
    return sorted(numbers)


def _run_git(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git without a shell and fail closed on missing history."""
    completed = subprocess.run(["git", *arguments], text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EnrichmentError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def enrich_changelog(changelog: str, prs: Iterable[Mapping[str, object]]) -> str:
    """Rebuild only the top-version rich block; older releases are immutable."""
    prs = list(prs)
    ranges = _version_ranges(changelog)
    if not ranges:
        return changelog
    start, end = ranges[0]
    top = changelog[start:end]
    older_numbers = _rich_numbers(changelog[end:])
    had_block = BLOCK_START in top
    top = re.sub(r"\n?<!-- project-toolkit:rich-block:start -->[\s\S]*?<!-- project-toolkit:rich-block:end -->\n?", "", top)
    # Compatibility with the original marker-only implementation.
    if not had_block:
        top = _remove_legacy_block(top)
    payload = _render_entries(prs, older_numbers)
    if payload:
        heading_end = top.find("\n")
        if heading_end < 0:
            heading_end = len(top)
        prefix = top[:heading_end].rstrip()
        suffix = top[heading_end:].lstrip("\n")
        top = prefix + "\n\n" + BLOCK_START + "\n" + payload + "\n" + BLOCK_END + "\n\n" + suffix
    return changelog[:start] + top + changelog[end:]


def enrich_release_body(body: str, rich_markdown: str) -> str:
    """Replace this tool's body block while preserving all Release Please text."""
    block = f"{BLOCK_START}\n{rich_markdown}\n{BLOCK_END}" if rich_markdown else ""
    pattern = rf"{re.escape(BLOCK_START)}[\s\S]*?{re.escape(BLOCK_END)}"
    if re.search(pattern, body):
        return re.sub(pattern, block, body)
    if not block:
        return body
    delimiters = list(re.finditer(r"\n---\n", body))
    if delimiters:
        footer_delimiter = delimiters[-1]
        return body[:footer_delimiter.start()] + "\n\n" + block + body[footer_delimiter.start():]
    return body.rstrip() + "\n\n" + block + "\n"


def enrich_component_release_body(body: str, rich_by_component: Mapping[str, str]) -> str:
    """Rebuild rich blocks inside Release Please multi-component details."""
    detail_pattern = re.compile(
        r"(?ms)^<details><summary>(?P<component>.+?): (?P<version>[0-9]+\.[0-9]+\.[0-9]+[^<]*)</summary>\n"
        r"(?P<notes>.*?)^</details>[ \t]*$"
    )
    matches = list(detail_pattern.finditer(body))
    if not matches:
        if rich_by_component:
            raise EnrichmentError("multi-component release body has no canonical component details")
        return enrich_release_body(body, "")

    found: set[str] = set()
    updated = body
    for match in reversed(matches):
        component = match.group("component")
        if component in found:
            raise EnrichmentError(f"release body contains duplicate component {component!r}")
        found.add(component)
        notes = match.group("notes")
        rich = rich_by_component.get(component, "")
        block = f"{BLOCK_START}\n{rich}\n{BLOCK_END}" if rich else ""
        block_pattern = rf"{re.escape(BLOCK_START)}[\s\S]*?{re.escape(BLOCK_END)}"
        if re.search(block_pattern, notes):
            new_notes = re.sub(block_pattern, block, notes)
        elif block:
            separator = "" if notes.endswith("\n\n") else ("\n" if notes.endswith("\n") else "\n\n")
            new_notes = notes + separator + block + "\n"
        else:
            new_notes = notes
        updated = updated[: match.start("notes")] + new_notes + updated[match.end("notes") :]

    missing = sorted(component for component, rich in rich_by_component.items() if rich and component not in found)
    if missing:
        raise EnrichmentError(
            "release body is missing configured component details: " + ", ".join(missing)
        )

    detail_ranges = [(match.start(), match.end()) for match in detail_pattern.finditer(updated)]
    block_pattern = re.compile(
        rf"{re.escape(BLOCK_START)}[\s\S]*?{re.escape(BLOCK_END)}"
    )
    for match in reversed(list(block_pattern.finditer(updated))):
        if not any(start <= match.start() and match.end() <= end for start, end in detail_ranges):
            updated = updated[: match.start()] + updated[match.end() :]
    return updated


def _safe_relative_path(value: str, *, label: str) -> Path:
    """Return a repository-relative path that cannot escape the checkout."""
    if not value or not SAFE_PATH_PATTERN.fullmatch(value):
        raise EnrichmentError(f"{label} must be a safe repository-relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise EnrichmentError(f"{label} must be a safe repository-relative path")
    return path


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """Load a JSON object or fail with release-specific context."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnrichmentError(f"{label} must contain a JSON object")
    return payload


def discover_release_targets(
    *,
    mode: str,
    package_path: str,
    config_file: Path,
    manifest_file: Path,
    config_backed_single: bool,
) -> list[tuple[str | None, Path]]:
    """Derive component/changelog pairs from Release Please's actual inputs."""
    if mode == "single" and not config_backed_single:
        root = _safe_relative_path(package_path, label="package path")
        return [(None, Path("CHANGELOG.md") if root == Path(".") else root / "CHANGELOG.md")]
    if mode not in {"single", "manifest"}:
        raise EnrichmentError("mode must be single or manifest")

    config = _load_json_object(config_file, label="Release Please config")
    manifest = _load_json_object(manifest_file, label="Release Please manifest")
    packages = config.get("packages")
    if not isinstance(packages, dict) or not packages or not manifest:
        raise EnrichmentError("Release Please config and manifest must define packages")

    targets: list[tuple[str | None, Path]] = []
    multi_component = mode == "manifest" and len(manifest) > 1
    for package in manifest:
        package_config = packages.get(package)
        if not isinstance(package, str) or not isinstance(package_config, dict):
            raise EnrichmentError(f"manifest package {package!r} is missing from config")
        if package_config.get("skip-changelog", config.get("skip-changelog", False)) is True:
            continue
        raw_changelog = package_config.get(
            "changelog-path", config.get("changelog-path", "CHANGELOG.md")
        )
        if not isinstance(raw_changelog, str):
            raise EnrichmentError(f"package {package!r} has an invalid changelog-path")
        root_relative = raw_changelog.startswith("/")
        changelog = _safe_relative_path(raw_changelog.lstrip("/"), label="changelog-path")
        package_root = _safe_relative_path(package, label="manifest package")
        if package_root != Path(".") and not root_relative:
            changelog = package_root / changelog
        component: str | None = None
        if multi_component:
            raw_component = package_config.get("package-name")
            if not isinstance(raw_component, str) or not raw_component.strip():
                raise EnrichmentError(
                    f"manifest package {package!r} needs package-name for release-body enrichment"
                )
            component = raw_component.strip()
        targets.append((component, changelog))
    if not targets:
        raise EnrichmentError("Release Please configuration does not produce a changelog")
    components = [component for component, _ in targets if component is not None]
    if len(components) != len(set(components)):
        raise EnrichmentError("Release Please package-name values must be unique")
    return sorted(targets, key=lambda target: ((target[0] or ""), target[1].as_posix()))


def discover_changelog_paths(
    *,
    mode: str,
    package_path: str,
    config_file: Path,
    manifest_file: Path,
    config_backed_single: bool,
) -> list[Path]:
    """Return the unique changelogs updated by Release Please."""
    return sorted(
        {
            path
            for _, path in discover_release_targets(
                mode=mode,
                package_path=package_path,
                config_file=config_file,
                manifest_file=manifest_file,
                config_backed_single=config_backed_single,
            )
        },
        key=lambda path: path.as_posix(),
    )


def source_pr_numbers(changelog: str, repository: str) -> list[int]:
    """Read only canonical Release Please PR attribution from the top version."""
    ranges = _version_ranges(changelog)
    if not ranges:
        raise EnrichmentError("changelog has no Release Please version section")
    start, end = ranges[0]
    top = changelog[start:end]
    escaped_repository = re.escape(repository)
    attribution = re.compile(
        rf"\(\[#(?P<number>[0-9]+)\]\(https://github\.com/{escaped_repository}/"
        rf"(?:issues|pull)/(?P=number)\)\)\s+"
        rf"\(\[[0-9a-f](7, 40)\]\(https://github\.com/{escaped_repository}/"
        rf"commit/[0-9a-f]40\)\)\s*$"
    )
    numbers: set[int] = set()
    fence: tuple[str, int] | None = None
    in_rich_block = False
    for line in top.splitlines():
        if line.strip() == BLOCK_START:
            in_rich_block = True
            continue
        if line.strip() == BLOCK_END:
            in_rich_block = False
            continue
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            token = fence_match.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is None and not in_rich_block:
            match = attribution.search(line)
            if match:
                numbers.add(int(match.group("number")))
    if in_rich_block or fence is not None:
        raise EnrichmentError("top changelog version contains an unterminated machine block or fence")
    return sorted(numbers)


def _run_gh(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run GitHub CLI without a shell, preserving its real response contract."""
    completed = subprocess.run(
        ["gh", *arguments], text=True, capture_output=True, check=False
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EnrichmentError(f"gh {' '.join(arguments)} failed: {detail}")
    return completed


def _gh_json(arguments: list[str]) -> Any:
    """Run a GitHub CLI command whose stdout is one JSON document."""
    completed = _run_gh(arguments)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EnrichmentError(f"gh {' '.join(arguments)} returned invalid JSON") from exc


def _validated_pull_request(payload: Any, repository: str, *, label: str) -> dict[str, Any]:
    """Validate a merged/same-repository source or same-repository release PR."""
    if not isinstance(payload, dict):
        raise EnrichmentError(f"{label} response must be a JSON object")
    head = payload.get("head")
    base = payload.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if not (
        isinstance(head_repo, dict)
        and isinstance(base_repo, dict)
        and head_repo.get("full_name") == repository
        and base_repo.get("full_name") == repository
    ):
        raise EnrichmentError(f"{label} repository validation failed")
    return payload


def prepare_release_enrichment(
    *,
    repository: str,
    release_prs_file: Path,
    mode: str,
    package_path: str,
    config_file: Path,
    manifest_file: Path,
    config_backed_single: bool,
    output_directory: Path,
) -> dict[str, Any]:
    """Check out one release PR, enrich its changelogs, and stage API payloads."""
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise EnrichmentError("repository must be exactly owner/name")
    try:
        release_prs = json.loads(release_prs_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"cannot read Release Please PR output: {exc}") from exc
    if not isinstance(release_prs, list) or len(release_prs) != 1:
        count = len(release_prs) if isinstance(release_prs, list) else "non-list"
        raise EnrichmentError(f"expected exactly one release PR, got {count}")
    release_number = release_prs[0].get("number") if isinstance(release_prs[0], dict) else None
    if not isinstance(release_number, int) or release_number <= 0:
        raise EnrichmentError("Release Please PR output has no valid number")

    release = _validated_pull_request(
        _gh_json(["api", f"repos/{repository}/pulls/{release_number}"]),
        repository,
        label=f"release PR {release_number}",
    )
    _run_gh(["pr", "checkout", str(release_number), "--repo", repository, "--force"])

    release_targets = discover_release_targets(
        mode=mode,
        package_path=package_path,
        config_file=config_file,
        manifest_file=manifest_file,
        config_backed_single=config_backed_single,
    )
    changelog_paths = sorted(
        {path for _, path in release_targets}, key=lambda path: path.as_posix()
    )
    numbers_by_path: dict[Path, list[int]] = {}
    legacy_by_path: dict[Path, set[int]] = {}
    legacy_numbers: set[int] = set()
    all_numbers: set[int] = set()
    for changelog_path in changelog_paths:
        try:
            changelog = changelog_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EnrichmentError(f"cannot read generated changelog {changelog_path}: {exc}") from exc
        numbers = source_pr_numbers(changelog, repository)
        numbers_by_path[changelog_path] = numbers
        legacy = set(legacy_dependency_pr_numbers(changelog)) - set(numbers)
        legacy_by_path[changelog_path] = legacy
        legacy_numbers.update(legacy)
        all_numbers.update(numbers)
        all_numbers.update(legacy)

    source_prs: dict[int, dict[str, Any]] = {}
    for number in sorted(all_numbers):
        source = _validated_pull_request(
            _gh_json(["api", f"repos/{repository}/pulls/{number}"]),
            repository,
            label=f"source PR {number}",
        )
        if source.get("state") != "closed" or not source.get("merged_at"):
            raise EnrichmentError(f"source PR {number} is not merged")
        if source.get("number") != number:
            raise EnrichmentError(f"source PR {number} response number does not match")
        source_prs[number] = {
            "number": number,
            "title": str(source.get("title", "")),
            "body": str(source.get("body") or ""),
            "legacy_dependency": number in legacy_numbers,
            "pr_url": str(source.get("html_url") or ""),
            "commit_sha": str(source.get("merge_commit_sha") or ""),
            "commit_url": (
                f"https://github.com/{repository}/commit/{source['merge_commit_sha']}"
                if source.get("merge_commit_sha") else ""
            ),
        }

    rendered_numbers: set[int] = set()
    for changelog_path in changelog_paths:
        original = changelog_path.read_text(encoding="utf-8")
        selected = [
            {**source_prs[number], "legacy_dependency": number in legacy_by_path[changelog_path]}
            for number in sorted(
                set(numbers_by_path[changelog_path]) | legacy_by_path[changelog_path]
            )
        ]
        updated = enrich_changelog(original, selected)
        changelog_path.write_text(updated, encoding="utf-8", newline="\n")
        top = _version_ranges(updated)
        if top:
            rendered_numbers.update(
                int(number) for number in _rich_numbers(updated[top[0][0] : top[0][1]])
            )

    release_body = str(release.get("body") or "")
    if mode == "manifest" and len(release_targets) > 1:
        rich_by_component: dict[str, str] = {}
        for component, path in release_targets:
            if component is None:
                raise EnrichmentError("multi-component release target has no component")
            changelog = path.read_text(encoding="utf-8")
            ranges = _version_ranges(changelog)
            path_numbers = sorted(
                int(number)
                for number in _rich_numbers(
                    changelog[ranges[0][0] : ranges[0][1]] if ranges else ""
                )
            )
            missing = [number for number in path_numbers if number not in source_prs]
            if missing:
                raise EnrichmentError(
                    f"generated rich markers reference unknown source PRs: {missing}"
                )
            rich_by_component[component] = _render_entries(
                [source_prs[number] for number in path_numbers], set()
            )
        updated_body = enrich_component_release_body(release_body, rich_by_component)
    else:
        missing = [number for number in rendered_numbers if number not in source_prs]
        if missing:
            raise EnrichmentError(f"generated rich markers reference unknown source PRs: {missing}")
        rich_markdown = _render_entries(
            [source_prs[number] for number in sorted(rendered_numbers)], set()
        )
        updated_body = enrich_release_body(release_body, rich_markdown)
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "changelog-paths.txt").write_text(
        "".join(f"{path.as_posix()}\n" for path in changelog_paths), encoding="utf-8"
    )
    (output_directory / "release-body.json").write_text(
        json.dumps({"body": updated_body}) + "\n", encoding="utf-8"
    )
    metadata = {
        "release_pr": release_number,
        "changelogs": [path.as_posix() for path in changelog_paths],
        "source_prs": sorted(all_numbers),
        "rendered_prs": sorted(rendered_numbers),
    }
    (output_directory / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="discover and enrich one Release Please PR")
    parser.add_argument("--repository")
    parser.add_argument("--release-prs", type=Path)
    parser.add_argument("--mode", default="single")
    parser.add_argument("--path", default=".")
    parser.add_argument("--config-file", type=Path, default=Path(".github/release-please/config.json"))
    parser.add_argument("--manifest-file", type=Path, default=Path(".github/release-please/manifest.json"))
    parser.add_argument("--config-backed-single", action="store_true")
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--changelog", type=Path)
    parser.add_argument("--pull-requests", type=Path, help="JSON array selected by Release Please")
    parser.add_argument("--release-body", type=Path)
    parser.add_argument("--rich-body", type=Path)
    args = parser.parse_args()
    if args.prepare:
        if not args.repository or not args.release_prs or not args.output_directory:
            parser.error("--prepare requires --repository, --release-prs, and --output-directory")
        prepare_release_enrichment(
            repository=args.repository,
            release_prs_file=args.release_prs,
            mode=args.mode,
            package_path=args.path,
            config_file=args.config_file,
            manifest_file=args.manifest_file,
            config_backed_single=args.config_backed_single,
            output_directory=args.output_directory,
        )
        return
    if not args.changelog or not args.pull_requests:
        parser.error("legacy mode requires --changelog and --pull-requests")
    prs = json.loads(args.pull_requests.read_text(encoding="utf-8"))
    original = args.changelog.read_text(encoding="utf-8")
    updated = enrich_changelog(original, prs)
    args.changelog.write_text(updated, encoding="utf-8", newline="\n")
    if args.release_body and args.rich_body:
        top = _version_ranges(updated)
        rich = _render_entries(prs, _rich_numbers(updated[top[0][1]:]) if top else set())
        args.rich_body.write_text(enrich_release_body(args.release_body.read_text(encoding="utf-8"), rich), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
