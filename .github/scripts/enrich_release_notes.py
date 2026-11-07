"""Render compact, deterministic release notes from Release Please output and merged PRs."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

# Level-2 PR body sections that feed the notes; aliases map to one key.
RICH_HEADINGS = {
    "release note": "release note",
    "highlight": "highlight",
    "usage example": "usage example",
    "migration": "migration",
    "breaking change": "migration",
    "breaking changes": "migration",
}
NOTES_MARKER = "<!-- project-toolkit:release-notes v2 -->"
# v1 left these in release PR bodies and top changelog sections.
BLOCK_START = "<!-- project-toolkit:rich-block:start -->"
BLOCK_END = "<!-- project-toolkit:rich-block:end -->"
LEGACY_MARKER_PATTERN = re.compile(r"^<!-- project-toolkit:rich-release-notes pr=\d+ -->\s*$")
LEGACY_RICH_HEADINGS = {
    "✨ highlights", "💡 usage examples", "🔄 migration",
    "highlights", "usage examples", "migration", "breaking changes",
}
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SAFE_PATH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
SAFE_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SAFE_TOKEN = re.compile(r"[\w@./:+-]{1,100}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{7,40}")
TITLE_PATTERN = re.compile(
    r"(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\r\n]*)\))?(?P<bang>!)?:[ \t]*(?P<subject>\S.*)"
)
DEPENDENCY_TITLE = re.compile(
    r"update (?:dependency |plugin |module )?(?P<name>\S+?)"
    r"(?: (?P<kind>docker tag|docker digest|action|digest|image))? to (?P<to>\S+?)"
    r"(?: \[security\])?"
)
RELEASE_COMMIT = re.compile(r"release\s+\S*\d+\.\d+\.\d+.*", re.IGNORECASE)
MAX_TABLE_ROWS = 50
MAX_LISTED_REFS = 5
MAX_NOTES_LENGTH = 60_000
INTERNAL_LABEL = "release-notes: internal"
PENDING_LABEL = "autorelease: pending"
RELEASE_BRANCH_PREFIX = "release-please--"

CATEGORIES = ("breaking", "new", "fixed", "faster", "updated", "reverted", "other")
SECTION_TITLES = {
    "breaking": "⚠️ Breaking changes",
    "new": "✨ New",
    "fixed": "🐛 Fixed",
    "faster": "🚀 Faster",
    "updated": "⬆️ Updated",
    "reverted": "↩️ Reverted",
    "other": "📝 Other changes",
}
GLANCE = {
    "breaking": "⚠️ {count} breaking",
    "new": "{count} new",
    "fixed": "{count} fixed",
    "faster": "{count} faster",
    "updated": "{count} updated",
    "reverted": "{count} reverted",
    "other": "{count} other",
    "internal": "{count} internal",
}
VISIBLE_TYPES = {"feat": "new", "fix": "fixed", "perf": "faster", "revert": "reverted"}
KNOWN_TYPES = {
    "feat", "fix", "perf", "revert", "deps", "chore", "docs", "ci",
    "test", "build", "style", "refactor",
}
# Release Please's default section titles, used when a repository has no config.
DEFAULT_SECTION_TYPES = {
    "features": "feat",
    "bug fixes": "fix",
    "performance improvements": "perf",
    "reverts": "revert",
    "miscellaneous chores": "chore",
    "documentation": "docs",
    "styles": "style",
    "code refactoring": "refactor",
    "tests": "test",
    "build system": "build",
    "continuous integration": "ci",
    "dependencies": "deps",
}
UNSAFE_HTML = re.compile(r"<(/?)(details|summary)", re.IGNORECASE)
# str.splitlines() also breaks on these; Release Please, Git and GitHub do not. Author
# text that kept one could otherwise start a forged `## x.y.z` changelog heading.
LINE_SEPARATORS = re.compile("[\r\u2028\u2029\x85\x0b\x0c\x1c-\x1e]")
# Outside code: an inline code span is kept, any other `<` is escaped so author
# text cannot open HTML (an unterminated `<!--` would hide the rest of the notes).
PROSE_HTML = re.compile(r"(`+)[^`\n](?:.*?[^`\n])?\1|`+|<")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


class EnrichmentError(RuntimeError):
    """Raised when release metadata is unsafe or ambiguous."""


class NotAPullRequest(EnrichmentError):
    """A `(#N)` reference that the API does not return as a pull request."""


# ---------------------------------------------------------------- PR bodies


def _lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split on newline only; see LINE_SEPARATORS."""
    parts = text.split("\n")
    if keepends:
        parts = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return parts


def _without_comments(lines: Iterable[str]) -> str:
    text = "\n".join(lines).strip()
    return re.sub(r"<!--[\s\S]*?-->", "", text).strip()


def _fence_step(line: str, fence: tuple[str, int] | None) -> tuple[bool, tuple[str, int] | None]:
    """Return whether the line is a fence delimiter and the fence state after it."""
    match = FENCE.match(line)
    if not match:
        return False, fence
    marker = match.group(1)
    if fence is None:
        return True, (marker[0], len(marker))
    if marker[0] == fence[0] and len(marker) >= fence[1]:
        return True, None
    return True, fence


def extract_rich_sections(body: str) -> dict[str, str]:
    """Extract the published level-2 sections, retaining Markdown and fences."""
    result: dict[str, str] = {}
    seen: set[str] = set()
    current: str | None = None
    fence: tuple[str, int] | None = None
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        is_fence, fence_after = _fence_step(line, fence)
        if is_fence or fence is not None:
            fence = fence_after
            if current is not None:
                result[current] += line + "\n"
            continue
        match = re.match(r"^##[ \t]+([^#].*?)[ \t]*$", line)
        if match:
            heading = match.group(1).strip().casefold()
            key = RICH_HEADINGS.get(heading)
            current = None
            if key is not None and heading not in seen:
                seen.add(heading)
                current = key
                if key in result:
                    result[key] += "\n"
                else:
                    result[key] = ""
            continue
        if current is not None:
            result[current] += line + "\n"
    # Trailing spaces would fail `git diff --check` once the notes are committed.
    cleaned = {
        key: "\n".join(line.rstrip() for line in _lines(value.strip()))
        for key, value in result.items()
    }
    return {key: value for key, value in cleaned.items() if _without_comments(_lines(value))}


def _neutralize(text: str) -> str:
    """Make author Markdown inert for Release Please and this tool, without dropping words."""
    text = LINE_SEPARATORS.sub(" ", text)
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            cleaned = re.sub(r"<!--[\s\S]*?-->", "", "\n".join(buffer))
            cleaned = PROSE_HTML.sub(lambda match: "&lt;" if match.group(0) == "<" else match.group(0), cleaned)
            lines.extend(cleaned.split("\n"))
            buffer.clear()

    for line in text.split("\n"):
        is_fence, fence_after = _fence_step(line, fence)
        if fence is None and not is_fence:
            buffer.append(line)
        else:
            flush()
            lines.append(line)
        fence = fence_after
    flush()
    if fence is not None:
        lines.append(fence[0] * fence[1])
    # A <details>/<summary> pair whose summary ends in x.y.z would be read by
    # Release Please as a component release and suppress the GitHub release.
    joined = "\n".join(line.rstrip() for line in lines)
    joined = UNSAFE_HTML.sub(lambda match: "&lt;" + match.group(1) + match.group(2), joined)
    # An HTML parser (Release Please reads component bodies with one) does not know
    # about code fences: an unclosed comment even inside code would swallow the rest.
    opened = joined.rfind("<!--")
    while opened >= 0 and "-->" not in joined[opened:]:
        joined = joined[:opened] + "&lt;!--" + joined[opened + 4 :]
        opened = joined.rfind("<!--", 0, opened)
    return re.sub(r"\n{3,}", "\n\n", joined).strip("\n")


def _one_line(text: str) -> tuple[str, str]:
    """Collapse prose to one line; return it and any fenced blocks it contained."""
    prose: list[str] = []
    fenced: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        is_fence, fence_after = _fence_step(line, fence)
        if is_fence or fence is not None:
            fenced.append(line)
        else:
            prose.append(line)
        fence = fence_after
    return " ".join(" ".join(prose).split()), "\n".join(fenced).strip("\n")


def _escape_title(title: str) -> str:
    title = " ".join(LINE_SEPARATORS.sub(" ", title).split()).replace("<", "&lt;").replace(">", "&gt;")
    return UNSAFE_HTML.sub(lambda match: "&lt;" + match.group(1) + match.group(2), title)


def _cell_text(cell: str) -> str:
    cell = cell.strip()
    link = re.match(r"\[`?([^\]`]+)`?\]\(", cell)
    if link:
        return link.group(1).strip()
    return re.split(r"\s+\(", cell, maxsplit=1)[0].strip().strip("`")


def renovate_rows(body: str) -> list[tuple[str, str, str]]:
    """Read (package, from, to) rows from the first Renovate `Package | Change` table."""
    lines = body.replace("\r\n", "\n").split("\n")
    rows: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = [_cell_text(cell).casefold() for cell in line.strip().strip("|").split("|")]
        if "package" not in header or "change" not in header:
            continue
        package_column = header.index("package")
        change_column = header.index("change")
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|") or len(rows) >= MAX_TABLE_ROWS:
                break
            cells = row.strip().strip("|").split("|")
            if len(cells) <= max(package_column, change_column):
                continue
            name = _cell_text(cells[package_column])
            change = re.search(r"`([^`]+)`\s*(?:→|->)\s*`([^`]+)`", cells[change_column])
            if not change or not SAFE_TOKEN.fullmatch(name):
                continue
            old, new = change.group(1).strip(), change.group(2).strip()
            if SAFE_TOKEN.fullmatch(old) and SAFE_TOKEN.fullmatch(new) and (name, old, new) not in rows:
                rows.append((name, old, new))
        break
    return rows


# --------------------------------------------------------------- entry model


class Entry:
    """One merged PR (or an unattributed line) as the renderer sees it."""

    def __init__(
        self,
        number: int | None,
        type: str,
        scope: str | None,
        subject: str,
        breaking: bool = False,
        labels: tuple[str, ...] = (),
        sections: dict[str, str] | None = None,
        body: str = "",
        order: str = "",
    ) -> None:
        self.number = number
        self.type = type
        self.scope = scope
        self.subject = subject
        self.breaking = breaking
        self.labels = labels
        self.sections = sections or {}
        self.body = body
        self.order = order
        self.footer: list[str] = []


def _parse_title(title: str) -> tuple[str, str | None, bool, str] | None:
    match = TITLE_PATTERN.fullmatch(" ".join(title.split()))
    if not match or match.group("type").casefold() not in KNOWN_TYPES:
        return None
    scope = (match.group("scope") or "").strip() or None
    return match.group("type").casefold(), scope, bool(match.group("bang")), match.group("subject").strip()


def make_entry(number: int, source: Mapping[str, object], fallback_type: str | None = None) -> Entry:
    """Build an entry from a merged PR's title, body and labels."""
    title = str(source.get("title") or "")
    parsed = _parse_title(title)
    labels_value = source.get("labels") or []
    labels = tuple(
        str(label.get("name") if isinstance(label, Mapping) else label)
        for label in labels_value
        if isinstance(labels_value, list)
    )
    body = str(source.get("body") or "")
    order = str(source.get("merged_at") or "")
    if parsed is None:
        return Entry(
            number, fallback_type or "other", None, _escape_title(title) or f"PR {number}",
            labels=labels, sections=extract_rich_sections(body), body=body, order=order,
        )
    kind, scope, bang, subject = parsed
    return Entry(
        number, kind, _escape_title(scope) if scope else None, _escape_title(subject), bang,
        labels, extract_rich_sections(body), body, order,
    )


def _section_types(changelog_sections: Sequence[Mapping[str, object]] | None) -> dict[str, str]:
    mapping = dict(DEFAULT_SECTION_TYPES)
    for section in changelog_sections or []:
        title, kind = section.get("section"), section.get("type")
        if isinstance(title, str) and isinstance(kind, str):
            mapping[_section_key(title)] = kind.casefold()
    return mapping


def _section_key(title: str) -> str:
    return re.sub(r"^[^\w]+", "", title.strip()).strip().casefold()


class ReleasePleaseNotes:
    """PRs Release Please listed in the top version, its breaking notes, and other lines."""

    def __init__(
        self,
        heading: str,
        listed: dict[int, str],
        breaking: dict[int, list[str]],
        unattributed: list[tuple[str | None, bool, str]],
        texts: dict[int, str] | None = None,
    ) -> None:
        self.heading = heading
        self.listed = listed
        self.breaking = breaking
        self.unattributed = unattributed
        self.texts = texts or {}


def _strip_legacy(top: str) -> str:
    """Remove v1 rich blocks and marker-only notes from a top version section."""
    top = re.sub(
        rf"\n?{re.escape(BLOCK_START)}[\s\S]*?{re.escape(BLOCK_END)}\n?", "\n", top
    )
    output: list[str] = []
    removing = False
    fence: tuple[str, int] | None = None
    for line in _lines(top, keepends=True):
        is_fence, fence_after = _fence_step(line, fence)
        if is_fence or fence is not None:
            fence = fence_after
            if not removing:
                output.append(line)
            continue
        if LEGACY_MARKER_PATTERN.match(line):
            removing = True
            continue
        if removing:
            heading = re.match(r"^###\s+(.+?)\s*$", line)
            if (heading and heading.group(1).strip().casefold() not in LEGACY_RICH_HEADINGS) or re.match(
                r"^##[ \t]+", line
            ):
                removing = False
        if not removing:
            output.append(line)
    return "".join(output)


def parse_release_please_notes(
    top: str, repository: str, section_types: Mapping[str, str] | None = None
) -> ReleasePleaseNotes:
    """Read Release Please's top version: listed PRs, breaking notes and unattributed lines."""
    types = section_types or DEFAULT_SECTION_TYPES
    top = _strip_legacy(top)
    lines = top.split("\n")
    heading = lines[0].rstrip()
    escaped = re.escape(repository)
    commit_link = r"\(\[[0-9a-f]{7,40}\]\(https://github\.com/" + escaped + r"/commit/[0-9a-f]{40}\)\)"
    attribution = re.compile(
        r"^(?P<text>[\s\S]*?)\s*\(\[#(?P<number>[0-9]+)\]\(https://github\.com/" + escaped
        + r"/(?:issues|pull)/(?P=number)\)\)(?:\s*" + commit_link + r")?\s*$"
    )
    commit_only = re.compile(r"\s*" + commit_link + r"\s*$")
    def scan(use_fences: bool) -> tuple[list[tuple[str | None, bool, str]], bool]:
        bullets: list[tuple[str | None, bool, str]] = []
        section: str | None = None
        breaking_section = False
        fence: tuple[str, int] | None = None
        for line in lines[1:]:
            is_fence, fence_after = _fence_step(line, fence) if use_fences else (False, None)
            if is_fence or fence is not None:
                fence = fence_after
                if bullets:
                    bullets[-1] = (bullets[-1][0], bullets[-1][1], bullets[-1][2] + "\n" + line.rstrip())
                continue
            header = re.match(r"^###[ \t]+(.+?)\s*$", line)
            if header:
                title = header.group(1)
                breaking_section = "breaking change" in title.casefold()
                section = None if breaking_section else types.get(_section_key(title), None)
                if not breaking_section and section is None:
                    section = "?" + _section_key(title)
                continue
            bullet = re.match(r"^[*-][ \t]+(.*)$", line)
            if bullet:
                bullets.append((section, breaking_section, bullet.group(1).rstrip()))
            elif line.strip() and bullets and line.startswith((" ", "\t")):
                bullets[-1] = (bullets[-1][0], bullets[-1][1], bullets[-1][2] + "\n" + line.strip())
            elif line.strip() and not line.startswith("<!--"):
                bullets.append((section, breaking_section, line.strip()))
        return bullets, fence is not None

    bullets, unterminated = scan(True)
    if unterminated:
        # A footer that opens a fence and never closes it is read as plain text.
        bullets, _ = scan(False)

    listed: dict[int, str] = {}
    texts: dict[int, str] = {}
    breaking: dict[int, list[str]] = {}
    unattributed: list[tuple[str | None, bool, str]] = []
    for kind, is_breaking, text in bullets:
        match = attribution.match(text)
        if match is None:
            unattributed.append((kind, is_breaking, commit_only.sub("", text)))
            continue
        number = int(match.group("number"))
        if is_breaking:
            note = re.sub(r"^\*\*[^*]+:\*\*\s*", "", match.group("text").strip())
            breaking.setdefault(number, []).append(note)
        else:
            listed.setdefault(number, kind or "other")
            texts.setdefault(number, match.group("text").strip())
    for number in breaking:
        listed.setdefault(number, "other")
    return ReleasePleaseNotes(heading, listed, breaking, unattributed, texts)


def build_entries(
    parsed: ReleasePleaseNotes,
    sources: Mapping[int, Mapping[str, object]],
    hidden: Iterable[Mapping[str, object]] = (),
) -> tuple[list[Entry], list[tuple[str | None, bool, str]]]:
    """Join Release Please's list with PR metadata and hidden-type commits."""
    entries: list[Entry] = []
    for number, section_type in sorted(parsed.listed.items()):
        source = sources.get(number, {})
        entry = make_entry(number, source, section_type if not section_type.startswith("?") else None)
        if not source:
            # Not a pull request (or not readable): keep Release Please's own text.
            text = parsed.texts.get(number, "")
            scoped = re.fullmatch(r"\*\*(?P<scope>[^*]+):\*\*\s*(?P<subject>.+)", text, re.DOTALL)
            entry.scope = _escape_title(scoped.group("scope")) if scoped else None
            entry.subject = _escape_title(scoped.group("subject") if scoped else text) or f"#{number}"
        if number in parsed.breaking:
            entry.breaking = True
            entry.footer = [note for note in parsed.breaking[number] if note]
        entries.append(entry)
    leftover: list[tuple[str | None, bool, str]] = []
    breaking_notes = [text for _, is_breaking, text in parsed.unattributed if is_breaking]
    others = [item for item in parsed.unattributed if not item[1]]
    if breaking_notes:
        candidates = [entry for entry in entries if entry.breaking and not entry.footer]
        if not candidates and not any(entry.breaking for entry in entries):
            candidates = [entry for entry in entries if "migration" in entry.sections]
        if len(candidates) == 1:
            candidates[0].breaking = True
            candidates[0].footer = breaking_notes
        else:
            leftover.extend((None, True, text) for text in breaking_notes)
    leftover.extend(others)
    known = {entry.number for entry in entries}
    for commit in hidden:
        number = commit.get("number")
        if not isinstance(number, int) or number in known:
            continue
        known.add(number)
        entry = make_entry(number, {**commit, **sources.get(number, {})})
        if not entry.breaking:
            # Release Please hid this type, so it is not user-facing unless breaking.
            entry.labels = (*entry.labels, INTERNAL_LABEL)
        entries.append(entry)
    return entries, leftover


def is_dependency(entry: Entry) -> bool:
    return entry.type == "deps" or (entry.scope or "").casefold() == "deps"


def _is_security(entry: Entry) -> bool:
    return "[security]" in entry.subject.casefold() or any(
        label.casefold() == "security" for label in entry.labels
    )


def classify(entry: Entry) -> str:
    """Return the section key for one entry: a visible category or `internal`."""
    if entry.breaking:
        return "breaking"
    if INTERNAL_LABEL in entry.labels:
        return "internal"
    if is_dependency(entry) and _is_security(entry):
        return "fixed"
    category = VISIBLE_TYPES.get(entry.type)
    if category is None:
        return "other" if entry.type == "other" else "internal"
    if is_dependency(entry) and entry.type in {"feat", "fix"}:
        return "updated"
    return category


# ----------------------------------------------------------------- rendering


def compress_refs(numbers: Iterable[int], link: Callable[[int], str]) -> str:
    """`[#1], [#3]–[#5]`: runs of three or more consecutive PRs become a range."""
    ordered = sorted(set(numbers))
    parts: list[str] = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{link(ordered[index])}–{link(ordered[end])}")
        else:
            parts.extend(link(number) for number in ordered[index : end + 1])
        index = end + 1
    return ", ".join(parts)


def _authored(entry: Entry) -> bool:
    return any(key in entry.sections for key in ("release note", "highlight", "usage example", "migration"))


def _dependency_rows(entry: Entry) -> list[tuple[str, str | None, str]]:
    rows = renovate_rows(entry.body)
    named = DEPENDENCY_TITLE.fullmatch(entry.subject)
    if named:
        name = named.group("name")
        if "digest" in (named.group("kind") or "") and ":" in name:
            name = name.rsplit(":", 1)[0]
        if not SAFE_TOKEN.fullmatch(name):
            return []
        if rows:
            preferred = [row for row in rows if row[0] == name] or [
                row for row in rows if not DIGEST_PATTERN.fullmatch(row[1])
            ] or rows
            return [(name, preferred[0][1], preferred[0][2])]
        target = named.group("to")
        return [(name, None, target)] if SAFE_TOKEN.fullmatch(target) else []
    return [(name, old, new) for name, old, new in rows]


def _major(version: str) -> int | None:
    match = re.match(r"v?(\d+)", version)
    return int(match.group(1)) if match else None


def ref_summary(numbers: Iterable[int], link: Callable[[int], str]) -> str:
    """Compressed references; more than MAX_LISTED_REFS become a count and the two endpoints."""
    ordered = sorted(set(numbers))
    if len(ordered) > MAX_LISTED_REFS:
        return f"{len(ordered)} PRs, {link(ordered[0])}–{link(ordered[-1])}"
    return compress_refs(ordered, link)


def _package_prefix(name: str) -> str:
    cut = max(name.rfind("/"), name.rfind(":"))
    return name[: cut + 1] if cut > 0 else ""


def dependency_lines(
    entries: Sequence[Entry], link: Callable[[int], str], *, compact: bool = False
) -> tuple[list[str], list[Entry]]:
    """One line per package with real from → to; entries without a package are returned.

    `compact` (the collapsed Internal list) joins packages that share a name prefix
    and the same change, and shortens long reference lists.
    """
    packages: dict[str, list[tuple[tuple[str, int], str | None, str]]] = {}
    refs: dict[str, set[int]] = {}
    rest: list[Entry] = []
    for entry in entries:
        rows = _dependency_rows(entry)
        if not rows or entry.number is None:
            rest.append(entry)
            continue
        for name, old, new in rows:
            packages.setdefault(name, []).append(((entry.order, entry.number), old, new))
            refs.setdefault(name, set()).add(entry.number)
    changes_by_key: dict[tuple[str, str, str], list[str]] = {}
    order: list[tuple[str, str, str]] = []
    for name in sorted(packages, key=lambda value: (value.casefold(), value)):
        changes = sorted(packages[name], key=lambda item: item[0])
        versions = [
            change for change in changes
            if not DIGEST_PATTERN.fullmatch(change[2]) and not (change[1] and DIGEST_PATTERN.fullmatch(change[1]))
        ]
        chosen = versions or changes
        old, new = chosen[0][1], chosen[-1][2]
        change = f"{old} → {new}" if old else f"→ {new}"
        if versions and old:
            before, after = _major(old), _major(new)
            if before is not None and after is not None and after > before:
                change += " · major"
        prefix = _package_prefix(name) if compact else ""
        key = (prefix, change, name if not prefix else "")
        if key not in changes_by_key:
            changes_by_key[key] = []
            order.append(key)
        changes_by_key[key].append(name)
    lines: list[str] = []
    for key in order:
        names = changes_by_key[key]
        numbers = {number for name in names for number in refs[name]}
        references = ref_summary(numbers, link) if compact else compress_refs(numbers, link)
        lines.append(f"- {', '.join(names)} {key[1]} ({references})")
    return lines, rest


def _indent(text: str, prefix: str) -> list[str]:
    return [(prefix + line).rstrip() if line else prefix.rstrip() for line in text.split("\n")]


def _headline(entry: Entry) -> tuple[str, str]:
    """Return the one-line headline and any fenced blocks from the release note."""
    note_source = entry.sections.get("release note") or entry.sections.get("highlight") or ""
    note, fenced = _one_line(_neutralize(note_source)) if note_source else ("", "")
    text = f"**{entry.scope}:** {entry.subject}" if entry.scope else entry.subject
    if note:
        text += f" — {note}"
    return text, fenced


def _entry_lines(entry: Entry, link: Callable[[int], str]) -> list[str]:
    headline, fenced = _headline(entry)
    ref = f" ({link(entry.number)})" if entry.number is not None else ""
    lines = [f"- {headline}{ref}"]
    for block in (fenced, _neutralize(entry.sections.get("usage example", ""))):
        if block:
            lines.extend(_indent(block, "  "))
    migration = _neutralize(entry.sections.get("migration", ""))
    if migration:
        lines.append("")
        lines.extend(_indent("**Migration**\n\n" + migration, "  > "))
    return lines


def _breaking_lines(entry: Entry, link: Callable[[int], str]) -> list[str]:
    headline, fenced = _headline(entry)
    ref = f" ({link(entry.number)})" if entry.number is not None else ""
    body: list[str] = [f"{headline}{ref}"]
    migration = _neutralize(entry.sections.get("migration", ""))
    footer = [
        note for note in (_neutralize(text) for text in entry.footer)
        if note and " ".join(note.split()) != " ".join(entry.subject.split())
    ]
    for block in (fenced, migration or "\n\n".join(footer), _neutralize(entry.sections.get("usage example", ""))):
        if block:
            body.extend(("", block))
    return ["> [!WARNING]", *_indent("\n".join(body), "> ")]


def render_notes(
    entries: Sequence[Entry],
    unattributed: Sequence[tuple[str | None, bool, str]],
    link: Callable[[int], str],
) -> str:
    """Render the notes that follow the version heading (marker included)."""
    groups: dict[str, list[Entry]] = {key: [] for key in (*CATEGORIES, "internal")}
    for entry in entries:
        groups[classify(entry)].append(entry)
    for key in groups:
        groups[key].sort(key=lambda item: ((item.scope or "").casefold(), item.subject.casefold(), item.number or 0))
    extra: dict[str, list[str]] = {key: [] for key in (*CATEGORIES, "internal")}
    for kind, is_breaking, text in unattributed:
        safe = _neutralize(text)
        if not safe:
            continue
        if is_breaking:
            extra["breaking"].append(safe)
        elif kind in VISIBLE_TYPES:
            extra[VISIBLE_TYPES[kind]].append(safe)
        elif kind in KNOWN_TYPES:
            extra["internal"].append(safe)
        else:
            extra["other"].append(safe)

    counts = {key: len(groups[key]) + len(extra[key]) for key in groups}
    glance = [GLANCE[key].format(count=counts[key]) for key in (*CATEGORIES, "internal") if counts[key]]
    blocks: list[str] = [NOTES_MARKER + ("\n**At a glance:** " + " · ".join(glance) if glance else "")]

    for key in CATEGORIES:
        lines: list[str] = []
        if key == "breaking":
            alerts = [_breaking_lines(entry, link) for entry in groups[key]]
            alerts += [["> [!WARNING]", *_indent(text, "> ")] for text in extra[key]]
            for alert in alerts:
                lines.extend((*alert, ""))
            lines = lines[:-1]
        else:
            plain = [entry for entry in groups[key] if not is_dependency(entry) or _authored(entry)]
            grouped = [entry for entry in groups[key] if entry not in plain]
            dependency, ungrouped = dependency_lines(grouped, link)
            for entry in sorted([*plain, *ungrouped], key=lambda item: ((item.scope or "").casefold(), item.subject.casefold(), item.number or 0)):
                lines.extend(_entry_lines(entry, link))
            lines.extend(dependency)
            lines.extend("- " + "\n  ".join(text.split("\n")) for text in extra[key])
        if lines:
            blocks.append(f"### {SECTION_TITLES[key]}\n\n" + "\n".join(lines))

    internal = groups["internal"]
    if internal or extra["internal"]:
        dependencies = [entry for entry in internal if is_dependency(entry) and not _authored(entry)]
        dependency, ungrouped = dependency_lines(dependencies, link, compact=True)
        maintenance = [entry for entry in internal if entry not in dependencies]
        merged: dict[tuple[str, ...], list[int]] = {}
        detailed: list[list[str]] = []
        for entry in [*maintenance, *ungrouped]:
            if _authored(entry) or entry.number is None:
                detailed.append(_entry_lines(entry, link))
                continue
            key_text = f"**{entry.scope}:** {entry.subject}" if entry.scope else entry.subject
            merged.setdefault((key_text.casefold(), key_text), []).append(entry.number)
        maintenance_lines = [
            f"- {text} ({ref_summary(numbers, link)})"
            for (_, text), numbers in sorted(merged.items(), key=lambda item: (item[0][0].lstrip("*"), item[0][0]))
        ]
        maintenance_lines.extend("- " + "\n  ".join(text.split("\n")) for text in extra["internal"])
        authored = [line for lines in detailed for line in lines]
        total = counts["internal"]
        summary = f"<details><summary>Internal changes ({total} {'PR' if total == 1 else 'PRs'})</summary>"

        def internal_block(dependency: list[str], maintenance: list[str], dropped: int) -> str:
            parts = [summary]
            if dependency:
                parts.append("**Dependencies**\n\n" + "\n".join(dependency))
            tail = [f"- … and {dropped} more; see the compare link in the heading"] if dropped else []
            if maintenance or authored or tail:
                parts.append("**Maintenance**\n\n" + "\n".join([*maintenance, *authored, *tail]))
            parts.append("</details>")
            return "\n\n".join(parts)

        # GitHub caps a PR body at 65,536 characters. Authored text is never cut:
        # one-line entries are dropped from the end instead.
        visible = "\n\n".join(blocks)
        dropped = 0
        while True:
            block = internal_block(dependency, maintenance_lines, dropped)
            if len(visible) + 2 + len(block) <= MAX_NOTES_LENGTH or not (dependency or maintenance_lines):
                break
            if maintenance_lines:
                maintenance_lines = maintenance_lines[:-1]
            else:
                dependency = dependency[:-1]
            dropped += 1
        blocks.append(block)
    rendered = "\n\n".join(blocks)
    if len(rendered) > MAX_NOTES_LENGTH:
        raise EnrichmentError(f"rendered release notes exceed {MAX_NOTES_LENGTH} characters")
    return rendered


def pull_link(repository: str) -> Callable[[int], str]:
    return lambda number: f"[#{number}](https://github.com/{repository}/pull/{number})"


# ----------------------------------------------------------- changelog/body


def _version_ranges(changelog: str) -> list[tuple[int, int]]:
    def scan(use_fences: bool) -> tuple[list[int], bool]:
        offsets: list[int] = []
        offset = 0
        fence: tuple[str, int] | None = None
        for line in _lines(changelog, keepends=True):
            is_fence, fence_after = _fence_step(line, fence) if use_fences else (False, None)
            if not is_fence and fence is None and re.match(r"^##[ \t]+.*$", line):
                offsets.append(offset)
            fence = fence_after
            offset += len(line)
        return offsets, fence is not None

    offsets, unterminated = scan(True)
    if unterminated:
        # An unclosed fence must not hide the older versions behind it.
        offsets, _ = scan(False)
    return [(start, offsets[i + 1] if i + 1 < len(offsets) else len(changelog)) for i, start in enumerate(offsets)]


def top_section(changelog: str) -> tuple[int, int, str]:
    ranges = _version_ranges(changelog)
    if not ranges:
        raise EnrichmentError("changelog has no Release Please version section")
    start, end = ranges[0]
    return start, end, changelog[start:end]


def rendered_notes_of(top: str) -> str | None:
    """Return the notes of an already rendered top section, or None."""
    heading, _, rest = top.partition("\n")
    rest = rest.strip("\n")
    if rest.split("\n", 1)[0] != NOTES_MARKER:
        return None
    return rest.rstrip()


def write_top_section(changelog: str, notes: str) -> str:
    start, end, top = top_section(changelog)
    heading = top.split("\n", 1)[0].rstrip()
    trailer = "\n\n" if end < len(changelog) else "\n"
    return changelog[:start] + heading + "\n\n" + notes + trailer + changelog[end:]


def render_changelog(
    changelog: str,
    repository: str,
    sources: Mapping[int, Mapping[str, object]],
    *,
    section_types: Mapping[str, str] | None = None,
    hidden: Iterable[Mapping[str, object]] = (),
) -> tuple[str, str]:
    """Return the updated changelog and the notes of its top version (a fixed point)."""
    _, _, top = top_section(changelog)
    existing = rendered_notes_of(top)
    if existing is not None:
        return changelog, existing
    parsed = parse_release_please_notes(top, repository, section_types)
    entries, unattributed = build_entries(parsed, sources, hidden)
    notes = render_notes(entries, unattributed, pull_link(repository))
    return write_top_section(changelog, notes), notes


def _heading_line(text: str) -> re.Match[str] | None:
    return re.search(r"(?m)^##[ \t]+\S.*$", text)


def replace_body_notes(body: str, heading: str, notes: str) -> str:
    """Replace the notes of a single-release PR body between its heading and footer."""
    lines = body.replace("\r\n", "\n").split("\n")
    if "---" not in lines:
        raise EnrichmentError("release PR body has no Release Please delimiter")
    first = lines.index("---")
    last = len(lines) - 1 - lines[::-1].index("---")
    if last == first:
        raise EnrichmentError("release PR body has no Release Please footer")
    content = "\n".join(lines[first + 1 : last])
    match = _heading_line(content)
    if match is None or match.group(0).strip() != heading.strip():
        raise EnrichmentError("release PR body and changelog disagree on the version heading")
    content = content[: match.end()] + "\n\n" + notes + "\n"
    return "\n".join([*lines[: first + 1], content, *lines[last:]])


def component_blocks(body: str) -> list[tuple[str, int, int]]:
    """Return (component, start, end) of each top-level component `<details>` block."""
    blocks: list[tuple[str, int, int]] = []
    depth = 0
    start = 0
    component = ""
    offset = 0
    for line in _lines(body, keepends=True):
        stripped = line.strip()
        if stripped.startswith("<details"):
            if depth == 0:
                match = re.fullmatch(
                    r"<details><summary>(?P<component>.+?): [0-9]+\.[0-9]+\.[0-9]+[^<]*</summary>", stripped
                )
                if match:
                    component = match.group("component")
                    start = offset + len(line)
                    depth = 1
            else:
                depth += 1
        elif stripped == "</details>" and depth:
            depth -= 1
            if depth == 0:
                blocks.append((component, start, offset))
        offset += len(line)
    return blocks


def release_body_components(body: str) -> set[str]:
    """Return components actually present in a Release Please manifest PR."""
    return {component for component, _, _ in component_blocks(body)}


def replace_component_notes(body: str, notes_by_component: Mapping[str, tuple[str, str]]) -> str:
    """Replace each component's notes inside its Release Please `<details>` block."""
    blocks = component_blocks(body)
    found = [component for component, _, _ in blocks]
    if len(found) != len(set(found)):
        raise EnrichmentError("release body contains a duplicate component")
    missing = sorted(set(notes_by_component) - set(found))
    if missing:
        raise EnrichmentError("release body is missing configured component details: " + ", ".join(missing))
    updated = body
    for component, start, end in reversed(blocks):
        if component not in notes_by_component:
            continue
        heading, notes = notes_by_component[component]
        inner = updated[start:end]
        match = _heading_line(inner)
        if match is None or match.group(0).strip() != heading.strip():
            raise EnrichmentError(f"component {component!r} body and changelog disagree on the version heading")
        updated = updated[:start] + inner[: match.end()] + "\n\n" + notes + "\n" + updated[end:]
    return updated


# ------------------------------------------------------------------ inputs


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


class ReleaseTarget:
    """One package Release Please versions: component, changelog, path and section types."""

    def __init__(
        self,
        component: str | None,
        changelog: Path,
        package: Path,
        section_types: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.component = component
        self.changelog = changelog
        self.package = package
        self.section_types = section_types


def discover_targets(
    *,
    mode: str,
    package_path: str,
    config_file: Path,
    manifest_file: Path,
    config_backed_single: bool,
) -> list[ReleaseTarget]:
    """Derive component, changelog and package path from Release Please's inputs."""
    if mode == "single" and not config_backed_single:
        root = _safe_relative_path(package_path, label="package path")
        changelog = Path("CHANGELOG.md") if root == Path(".") else root / "CHANGELOG.md"
        return [ReleaseTarget(None, changelog, root)]
    if mode not in {"single", "manifest"}:
        raise EnrichmentError("mode must be single or manifest")

    config = _load_json_object(config_file, label="Release Please config")
    manifest = _load_json_object(manifest_file, label="Release Please manifest")
    packages = config.get("packages")
    if not isinstance(packages, dict) or not packages or not manifest:
        raise EnrichmentError("Release Please config and manifest must define packages")

    targets: list[ReleaseTarget] = []
    multi_component = mode == "manifest" and len(manifest) > 1
    for package in manifest:
        package_config = packages.get(package)
        if not isinstance(package, str) or not isinstance(package_config, dict):
            raise EnrichmentError(f"manifest package {package!r} is missing from config")
        if package_config.get("skip-changelog", config.get("skip-changelog", False)) is True:
            continue
        raw_changelog = package_config.get("changelog-path", config.get("changelog-path", "CHANGELOG.md"))
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
                raise EnrichmentError(f"manifest package {package!r} needs package-name for release-body enrichment")
            component = raw_component.strip()
        sections = package_config.get("changelog-sections", config.get("changelog-sections"))
        types = _section_types(sections if isinstance(sections, list) else None)
        targets.append(ReleaseTarget(component, changelog, package_root, tuple(sorted(types.items()))))
    if not targets:
        raise EnrichmentError("Release Please configuration does not produce a changelog")
    components = [target.component for target in targets if target.component is not None]
    if len(components) != len(set(components)):
        raise EnrichmentError("Release Please package-name values must be unique")
    return sorted(targets, key=lambda target: (target.component or "", target.changelog.as_posix()))


def discover_release_targets(**kwargs: Any) -> list[tuple[str | None, Path]]:
    """Component/changelog pairs (kept for callers of the v1 interface)."""
    return [(target.component, target.changelog) for target in discover_targets(**kwargs)]


def discover_changelog_paths(**kwargs: Any) -> list[Path]:
    """Return the unique changelogs updated by Release Please."""
    return sorted({target.changelog for target in discover_targets(**kwargs)}, key=lambda path: path.as_posix())


def source_pr_numbers(changelog: str, repository: str) -> list[int]:
    """Read only canonical Release Please PR attribution from the top version."""
    _, _, top = top_section(changelog)
    parsed = parse_release_please_notes(top, repository)
    return sorted(parsed.listed)


# -------------------------------------------------------------- git / gh


def _run_gh(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run GitHub CLI without a shell, preserving its real response contract."""
    completed = subprocess.run(["gh", *arguments], text=True, capture_output=True, check=False)
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


def _run_git(arguments: list[str]) -> str:
    completed = subprocess.run(["git", *arguments], text=True, capture_output=True, check=False)
    if completed.returncode:
        raise EnrichmentError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout


def previous_tag(heading: str, repository: str) -> str | None:
    """Read the previous tag from Release Please's compare link, if there is one."""
    match = re.match(
        rf"^##[ \t]+\[[^\]]+\]\(https://github\.com/{re.escape(repository)}/compare/(?P<prev>[^)\s]+?)\.\.\.[^)\s]+\)",
        heading,
    )
    if not match:
        return None
    tag = match.group("prev")
    if not SAFE_REF_PATTERN.fullmatch(tag) or ".." in tag:
        raise EnrichmentError("previous tag in the changelog heading is not a safe ref")
    return tag


def hidden_commits(
    *, previous: str, base_sha: str, package: Path, listed: Iterable[int], exclude: Iterable[Path] = ()
) -> list[dict[str, object]]:
    """Recover merged PRs Release Please hid (docs, ci, test, ...) between two revisions."""
    if not SHA_PATTERN.fullmatch(base_sha):
        raise EnrichmentError("release PR base SHA is invalid")
    if not SAFE_REF_PATTERN.fullmatch(previous) or ".." in previous:
        raise EnrichmentError("previous tag is not a safe ref")
    start = _run_git(["rev-parse", "--verify", "--quiet", "refs/tags/" + previous + "^" + "{commit}"]).strip()
    if not SHA_PATTERN.fullmatch(start):
        raise EnrichmentError(f"previous tag {previous} does not resolve to a commit")
    arguments = ["log", "--format=%H%x00%s", f"{start}..{base_sha}"]
    excluded = sorted({path.as_posix() for path in exclude if path != Path(".")})
    if package != Path(".") or excluded:
        # Release Please gives a commit to the most specific package, so nested or
        # sibling component paths are not this package's history.
        arguments += ["--", package.as_posix(), *(f":(exclude){path}" for path in excluded)]
    seen = set(listed)
    commits: list[dict[str, object]] = []
    for line in _lines(_run_git(arguments)):
        _, _, subject = line.partition("\x00")
        match = re.fullmatch(r"(?P<title>.+?) \(#(?P<number>[0-9]+)\)", subject.strip())
        if not match:
            continue
        number = int(match.group("number"))
        parsed = _parse_title(match.group("title"))
        if number in seen or parsed is None or RELEASE_COMMIT.fullmatch(parsed[3]):
            continue
        seen.add(number)
        commits.append({"number": number, "title": match.group("title")})
    return sorted(commits, key=lambda commit: int(str(commit["number"])))


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


def _pending_release_pr(repository: str, target_branch: str) -> int | None:
    """Find the one open `autorelease: pending` PR Release Please left unchanged."""
    arguments = [
        "pr", "list", "--repo", repository, "--state", "open", "--label", PENDING_LABEL,
        "--json", "number,headRefName,baseRefName,isCrossRepository", "--limit", "20",
    ]
    if target_branch:
        arguments += ["--base", target_branch]
    payload = _gh_json(arguments)
    if not isinstance(payload, list):
        raise EnrichmentError("gh pr list returned a non-list")
    candidates = [
        item for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("number"), int)
        and item.get("isCrossRepository") is False
        and str(item.get("headRefName", "")).startswith(RELEASE_BRANCH_PREFIX)
        and (not target_branch or item.get("baseRefName") == target_branch)
    ]
    if not candidates:
        print(f"::notice::No open '{PENDING_LABEL}' release PR; nothing to enrich")
        return None
    if len(candidates) > 1:
        numbers = ", ".join(f"#{item['number']}" for item in candidates)
        print(f"::warning::Several open '{PENDING_LABEL}' release PRs ({numbers}); not enriching any")
        return None
    return int(candidates[0]["number"])


def _source_pr(repository: str, number: int) -> dict[str, Any]:
    """Fetch one merged PR into this repository; a fork head is allowed (titles are escaped)."""
    try:
        source = _gh_json(["api", f"repos/{repository}/pulls/{number}"])
    except EnrichmentError as error:
        raise NotAPullRequest(f"#{number} is not a readable pull request ({error})") from error
    if not isinstance(source, dict):
        raise EnrichmentError(f"source PR {number} response must be a JSON object")
    base = source.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if not isinstance(base_repo, dict) or base_repo.get("full_name") != repository:
        raise EnrichmentError(f"source PR {number} repository validation failed")
    if source.get("state") != "closed" or not source.get("merged_at"):
        raise EnrichmentError(f"source PR {number} is not merged")
    if source.get("number") != number:
        raise EnrichmentError(f"source PR {number} response number does not match")
    head = source.get("head")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != repository:
        print(f"::warning::source PR {number} came from a fork; rendering it from its title and body")
    return source


def _source_prs(
    repository: str, numbers: Iterable[int], *, strict: bool = True
) -> dict[int, dict[str, object]]:
    sources: dict[int, dict[str, object]] = {}
    for number in sorted(set(numbers)):
        try:
            source = _source_pr(repository, number)
        except EnrichmentError as error:
            if strict and not isinstance(error, NotAPullRequest):
                raise
            # A hidden commit's `(#N)` may not be a merged PR; keep its commit subject.
            print(f"::warning::{error}; listing #{number} from its commit subject")
            continue
        labels = source.get("labels") or []
        sources[number] = {
            "number": number,
            "title": str(source.get("title", "")),
            "body": str(source.get("body") or ""),
            "labels": [str(label.get("name", "")) for label in labels if isinstance(label, dict)],
            "merged_at": str(source.get("merged_at") or ""),
        }
    return sources


def _write_outputs(output_directory: Path, changelogs: Iterable[Path], body: str | None, metadata: dict[str, Any]) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "changelog-paths.txt").write_text(
        "".join(f"{path.as_posix()}\n" for path in changelogs), encoding="utf-8"
    )
    (output_directory / "release-body.json").write_text(json.dumps({"body": body or ""}) + "\n", encoding="utf-8")
    (output_directory / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    target_branch: str = "",
) -> dict[str, Any]:
    """Check out the release PR, render its changelogs, and stage the PR body update."""
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise EnrichmentError("repository must be exactly owner/name")
    try:
        release_prs = json.loads(release_prs_file.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"cannot read Release Please PR output: {exc}") from exc
    if not isinstance(release_prs, list) or len(release_prs) > 1:
        count = len(release_prs) if isinstance(release_prs, list) else "non-list"
        raise EnrichmentError(f"expected exactly one release PR, got {count}")
    if release_prs:
        release_number = release_prs[0].get("number") if isinstance(release_prs[0], dict) else None
        if not isinstance(release_number, int) or release_number <= 0:
            raise EnrichmentError("Release Please PR output has no valid number")
    else:
        # Release Please skips an open PR whose generated body did not change and
        # then reports no PR; enrich the pending one so it still gets v2 notes.
        release_number = _pending_release_pr(repository, target_branch)
        if release_number is None:
            metadata = {"skipped": True, "release_pr": 0, "body_changed": False}
            _write_outputs(output_directory, [], None, metadata)
            return metadata

    release = _validated_pull_request(
        _gh_json(["api", f"repos/{repository}/pulls/{release_number}"]), repository,
        label=f"release PR {release_number}",
    )
    head = release.get("head") or {}
    if not str(head.get("ref", "")).startswith(RELEASE_BRANCH_PREFIX) or release.get("state", "open") != "open":
        raise EnrichmentError(f"release PR {release_number} is not an open Release Please branch")
    if target_branch and (release.get("base") or {}).get("ref") != target_branch:
        raise EnrichmentError(f"release PR {release_number} does not target {target_branch}")
    base_sha = str((release.get("base") or {}).get("sha") or "")
    if not SHA_PATTERN.fullmatch(base_sha):
        raise EnrichmentError(f"release PR {release_number} has no valid base SHA")
    _run_gh(["pr", "checkout", str(release_number), "--repo", repository, "--force"])

    release_body = str(release.get("body") or "")
    targets = discover_targets(
        mode=mode, package_path=package_path, config_file=config_file,
        manifest_file=manifest_file, config_backed_single=config_backed_single,
    )
    multi_component = mode == "manifest" and len(targets) > 1
    packages = [target.package for target in targets]
    if multi_component:
        active = release_body_components(release_body)
        targets = [target for target in targets if target.component in active]
        if not targets:
            raise EnrichmentError("multi-component release body has no canonical component details")

    notes_by_component: dict[str, tuple[str, str]] = {}
    single: tuple[str, str] | None = None
    all_sources: set[int] = set()
    hidden_numbers: set[int] = set()
    rendered_paths: dict[Path, tuple[str, str]] = {}
    for target in targets:
        if target.changelog in rendered_paths:
            heading, notes = rendered_paths[target.changelog]
        else:
            try:
                original = target.changelog.read_text(encoding="utf-8")
            except OSError as exc:
                raise EnrichmentError(f"cannot read generated changelog {target.changelog}: {exc}") from exc
            _, _, top = top_section(original)
            heading = top.split("\n", 1)[0].rstrip()
            if rendered_notes_of(top) is None:
                types = dict(target.section_types) or None
                parsed = parse_release_please_notes(top, repository, types)
                sources = _source_prs(repository, parsed.listed)
                all_sources.update(sources)
                previous = previous_tag(heading, repository)
                nested = [
                    package for package in packages
                    if package != target.package
                    and target.package in package.parents
                ]
                hidden = (
                    hidden_commits(
                        previous=previous, base_sha=base_sha, package=target.package,
                        listed=parsed.listed, exclude=nested,
                    )
                    if previous else []
                )
                hidden_numbers.update(int(str(commit["number"])) for commit in hidden)
                # Hidden-type PRs can still carry a Migration or a Release note.
                hidden_sources = _source_prs(repository, [int(str(commit["number"])) for commit in hidden], strict=False)
                sources = {**hidden_sources, **sources}
                updated, notes = render_changelog(original, repository, sources, section_types=types, hidden=hidden)
                with target.changelog.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(updated)
            else:
                _, notes = render_changelog(original, repository, {})
            rendered_paths[target.changelog] = (heading, notes)
        if multi_component:
            notes_by_component[str(target.component)] = (heading, notes)
        else:
            single = (heading, notes)

    if multi_component:
        updated_body = replace_component_notes(release_body, notes_by_component)
    else:
        assert single is not None
        updated_body = replace_body_notes(release_body, *single)
    changelogs = sorted(rendered_paths, key=lambda path: path.as_posix())
    metadata = {
        "skipped": False,
        "release_pr": release_number,
        "changelogs": [path.as_posix() for path in changelogs],
        "source_prs": sorted(all_sources),
        "hidden_prs": sorted(hidden_numbers),
        "body_changed": updated_body != release_body,
    }
    _write_outputs(output_directory, changelogs, updated_body, metadata)
    return metadata


def sync_component_releases(
    *,
    repository: str,
    outputs: Mapping[str, Any],
    mode: str,
    config_file: Path,
    manifest_file: Path,
    notes_directory: Path,
) -> list[str]:
    """Reset each released component's GitHub release notes to its CHANGELOG section.

    Release Please builds multi-component release notes from the PR body's
    `textContent`, which flattens the collapsed Internal list. Single-package
    releases keep the body verbatim and are left alone.
    """
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise EnrichmentError("repository must be exactly owner/name")
    if mode != "manifest":
        return []
    # The release is already tagged: a problem here is reported, never fatal.
    try:
        manifest = _load_json_object(manifest_file, label="Release Please manifest")
        if len(manifest) < 2:
            return []
        targets = discover_targets(
            mode=mode, package_path=".", config_file=config_file,
            manifest_file=manifest_file, config_backed_single=False,
        )
        released = json.loads(str(outputs.get("paths_released") or "[]"))
        if not isinstance(released, list):
            raise EnrichmentError("paths_released must be a list")
    except (EnrichmentError, json.JSONDecodeError) as error:
        print(f"::warning::Component release notes were not synced: {error}")
        return []
    by_package = {target.package.as_posix(): target for target in targets}
    notes_directory.mkdir(parents=True, exist_ok=True)
    synced: list[str] = []
    for path in released:
        try:
            package = _safe_relative_path(str(path), label="released path").as_posix()
            target = by_package.get(package)
            if target is None:
                print(f"::notice::{package} has no changelog; its release notes are left as Release Please wrote them")
                continue
            tag = str(outputs.get(f"{path}--tag_name") or "")
            version = str(outputs.get(f"{path}--version") or "")
            if not SAFE_REF_PATTERN.fullmatch(tag) or ".." in tag or not re.fullmatch(r"\d+\.\d+\.\d+\S*", version):
                raise EnrichmentError(f"release outputs for {package!r} are incomplete or unsafe")
            try:
                changelog = target.changelog.read_text(encoding="utf-8")
            except OSError as exc:
                raise EnrichmentError(f"cannot read {target.changelog}: {exc}") from exc
            _, _, top = top_section(changelog)
            heading = top.split("\n", 1)[0]
            if not re.match(rf"^##[ \t]+\[?{re.escape(version)}\]?", heading):
                raise EnrichmentError(f"{target.changelog} top section is not version {version}")
            if rendered_notes_of(top) is None:
                continue
            notes_file = notes_directory / f"{len(synced)}.md"
            notes_file.write_text(top.rstrip() + "\n", encoding="utf-8")
            _run_gh(["release", "edit", tag, "--repo", repository, "--notes-file", str(notes_file)])
            synced.append(tag)
        except EnrichmentError as error:
            print(f"::warning::Release notes of {path!r} were not synced: {error}")
    return synced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="render the open Release Please PR")
    parser.add_argument("--sync-releases", action="store_true", help="reset component GitHub releases")
    parser.add_argument("--repository")
    parser.add_argument("--release-prs", type=Path)
    parser.add_argument("--release-outputs", type=Path, help="JSON of the Release Please action outputs")
    parser.add_argument("--mode", default="single")
    parser.add_argument("--path", default=".")
    parser.add_argument("--config-file", type=Path, default=Path(".github/release-please/config.json"))
    parser.add_argument("--manifest-file", type=Path, default=Path(".github/release-please/manifest.json"))
    parser.add_argument("--config-backed-single", action="store_true")
    parser.add_argument("--target-branch", default="", help="release PR base branch for the pending-PR fallback")
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--changelog", type=Path)
    parser.add_argument("--pull-requests", type=Path, help="JSON array of merged PRs for --changelog")
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
            target_branch=args.target_branch,
        )
        return
    if args.sync_releases:
        if not args.repository or not args.release_outputs or not args.output_directory:
            parser.error("--sync-releases requires --repository, --release-outputs, and --output-directory")
        outputs = _load_json_object(args.release_outputs, label="Release Please outputs")
        synced = sync_component_releases(
            repository=args.repository, outputs=outputs, mode=args.mode,
            config_file=args.config_file, manifest_file=args.manifest_file,
            notes_directory=args.output_directory,
        )
        print("Synced component releases: " + (", ".join(synced) or "none"))
        return
    if not args.changelog or not args.pull_requests or not args.repository:
        parser.error("--changelog requires --pull-requests and --repository")
    prs = json.loads(args.pull_requests.read_text(encoding="utf-8"))
    sources = {int(pr["number"]): pr for pr in prs if isinstance(pr, dict) and str(pr.get("number", "")).isdigit()}
    updated, _ = render_changelog(args.changelog.read_text(encoding="utf-8"), args.repository, sources)
    with args.changelog.open("w", encoding="utf-8", newline="\n") as changelog_file:
        changelog_file.write(updated)


if __name__ == "__main__":
    try:
        main()
    except EnrichmentError as error:
        raise SystemExit(f"enrich_release_notes: {error}") from error
