"""
Changelog assembly: parse PRs, group them, and render the output formats.

Pure functions over GitHub/Linear payloads -- no network calls, no connector
objects -- so the grouping and rendering rules stay testable in isolation
from Scalekit.

Grouping follows Conventional Commits (feat:/fix:/chore: ...) because that is
the convention most repos already tag PR titles with. When a title carries no
prefix, the PR's labels are consulted, and only then does it fall back to a
catch-all bucket -- so an unprefixed repo still produces a useful changelog
rather than dumping everything into "Other".
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Display order and headings for the changelog sections.
SECTIONS = [
    ("feature", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("docs", "Documentation"),
    ("chore", "Chores & Maintenance"),
    ("other", "Other Changes"),
]

# Conventional Commit type -> section key. Several types deliberately collapse
# into one bucket (refactor/style/test/build/ci all read as maintenance to a
# release audience, even though they differ to a developer).
TYPE_TO_SECTION = {
    "feat": "feature",
    "feature": "feature",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "perf": "perf",
    "performance": "perf",
    "docs": "docs",
    "doc": "docs",
    "chore": "chore",
    "refactor": "chore",
    "style": "chore",
    "test": "chore",
    "tests": "chore",
    "build": "chore",
    "ci": "chore",
    "deps": "chore",
    "revert": "other",
}

# Label names that imply a section when the title has no prefix. Matched as
# substrings against lowercased label names, so "type: bug" hits "bug".
LABEL_HINTS = [
    ("feature", "feature"),
    ("enhancement", "feature"),
    ("bug", "fix"),
    ("fix", "fix"),
    ("performance", "perf"),
    ("documentation", "docs"),
    ("docs", "docs"),
    ("dependencies", "chore"),
    ("chore", "chore"),
    ("maintenance", "chore"),
]

# Conventional Commit prefix: type(optional scope)!: description
_CONVENTIONAL = re.compile(
    r"^\s*(?P<type>[a-zA-Z]+)\s*(?:\((?P<scope>[^)]*)\))?\s*(?P<breaking>!)?\s*:\s*(?P<rest>.+)$"
)

# Bracketed prefix style, e.g. "[chore] update collector" or "[FIX] login".
# Common in repos that predate Conventional Commits -- verified against
# open-telemetry/opentelemetry-demo, where "[chore] add build-and-push env
# files" would otherwise fall through to the catch-all bucket.
_BRACKETED = re.compile(r"^\s*\[(?P<type>[a-zA-Z]+)\]\s*(?P<rest>.+)$")

# A leading "[scope]" that names a component rather than a change type, e.g.
# "[shippingservice] add grpc conventions". Stripped before the verb check so
# the verb is actually the first word.
_BRACKET_SCOPE = re.compile(r"^\s*\[[^\]]*\]\s*")

# Leading imperative verbs, for repos with no prefix convention at all.
# Deliberately conservative: only unambiguous verbs are listed, so a title
# the agent cannot confidently place stays in "Other Changes" rather than
# being filed somewhere misleading.
VERB_HINTS = {
    "add": "feature",
    "added": "feature",
    "adds": "feature",
    "implement": "feature",
    "implemented": "feature",
    "introduce": "feature",
    "support": "feature",
    "fix": "fix",
    "fixed": "fix",
    "fixes": "fix",
    "resolve": "fix",
    "correct": "fix",
    "patch": "fix",
    "optimize": "perf",
    "optimise": "perf",
    "speed": "perf",
    "document": "docs",
    "docs": "docs",
    "update": "chore",
    "updated": "chore",
    "updates": "chore",
    "bump": "chore",
    "upgrade": "chore",
    "remove": "chore",
    "removed": "chore",
    "refactor": "chore",
    "rename": "chore",
    "align": "chore",
    "format": "chore",
    "move": "chore",
    "build": "chore",
}

# Squash-merge suffix GitHub appends to commit messages, e.g. "Fix thing (#123)".
_PR_NUMBER_IN_MESSAGE = re.compile(r"\(#(\d+)\)\s*$")


@dataclass
class ChangelogEntry:
    """One merged PR, classified and ready to render."""

    number: int
    title: str            # cleaned title, prefix stripped
    raw_title: str
    url: str
    author: str
    merged_at: str
    section: str
    breaking: bool = False
    scope: str = ""
    labels: List[str] = field(default_factory=list)
    linear_ids: List[str] = field(default_factory=list)
    linear_issues: List[Dict] = field(default_factory=list)


def _linear_pattern(team_prefixes: Optional[List[str]] = None) -> "re.Pattern":
    """
    Build the regex that spots Linear identifiers like ENG-123.

    With configured prefixes the match is exact, which avoids false positives
    on things like "UTF-8" or "CVE-2026". With none configured it falls back
    to any 2-5 letter uppercase prefix, which is the common Linear shape.
    """
    if team_prefixes:
        alternatives = "|".join(re.escape(p.upper()) for p in team_prefixes)
        return re.compile(rf"\b({alternatives})-(\d+)\b", re.IGNORECASE)
    return re.compile(r"\b([A-Z][A-Z0-9]{1,4})-(\d+)\b")


def extract_linear_ids(text: str, team_prefixes: Optional[List[str]] = None) -> List[str]:
    """
    Pull Linear issue identifiers out of a PR title, body, or branch name.

    Returns them uppercased and de-duplicated, preserving first-seen order so
    the primary issue (usually named first in a title) stays first.
    """
    if not text:
        return []
    found: List[str] = []
    for match in _linear_pattern(team_prefixes).finditer(text):
        identifier = f"{match.group(1).upper()}-{match.group(2)}"
        if identifier not in found:
            found.append(identifier)
    return found


def classify_pull_request(
    pr: Dict, team_prefixes: Optional[List[str]] = None
) -> Optional[ChangelogEntry]:
    """
    Convert one merged PR payload into a ChangelogEntry.

    Returns None for a payload with no usable title or number, so one
    malformed record is skipped rather than failing the whole run.
    """
    if not isinstance(pr, dict):
        return None

    raw_title = str(pr.get("title") or "").strip()
    number = pr.get("number")
    if not raw_title or number is None:
        return None

    try:
        number = int(number)
    except (TypeError, ValueError):
        return None

    labels = [
        str(l.get("name", "")).strip()
        for l in (pr.get("labels") or [])
        if isinstance(l, dict) and l.get("name")
    ]

    section, title, breaking, scope = _classify_title(raw_title, labels)

    # Linear ids can appear in the title, the body, or the branch name; a
    # branch like "eng-412-fix-login" is extremely common and is the only
    # signal when the title itself is clean prose.
    head_ref = str(((pr.get("head") or {}) if isinstance(pr.get("head"), dict) else {}).get("ref") or "")
    haystack = " ".join([raw_title, str(pr.get("body") or "")[:2000], head_ref])
    linear_ids = extract_linear_ids(haystack, team_prefixes)

    user = pr.get("user") or {}
    return ChangelogEntry(
        number=number,
        title=title,
        raw_title=raw_title,
        url=str(pr.get("html_url") or ""),
        author=str(user.get("login") or "") if isinstance(user, dict) else "",
        merged_at=str(pr.get("merged_at") or ""),
        section=section,
        breaking=breaking,
        scope=scope,
        labels=labels,
        linear_ids=linear_ids,
    )


def _classify_title(raw_title: str, labels: List[str]):
    """Return (section, cleaned_title, breaking, scope) for a PR title."""
    match = _CONVENTIONAL.match(raw_title)
    if match:
        commit_type = (match.group("type") or "").lower()
        section = TYPE_TO_SECTION.get(commit_type)
        if section:
            title = match.group("rest").strip()
            breaking = bool(match.group("breaking"))
            scope = (match.group("scope") or "").strip()
            # A "BREAKING CHANGE" note anywhere also counts, per the spec.
            breaking = breaking or "breaking change" in raw_title.lower()
            return section, title or raw_title, breaking, scope

    bracketed = _BRACKETED.match(raw_title)
    if bracketed:
        commit_type = (bracketed.group("type") or "").lower()
        section = TYPE_TO_SECTION.get(commit_type)
        if section:
            title = bracketed.group("rest").strip()
            return section, title or raw_title, "breaking change" in raw_title.lower(), ""

    # No usable prefix -- fall back to labels before giving up.
    lowered = [l.lower() for l in labels]
    for hint, section in LABEL_HINTS:
        if any(hint in label for label in lowered):
            return section, raw_title, "breaking change" in raw_title.lower(), ""

    # Last resort: the leading verb. Many repos never adopted a prefix
    # convention but still write imperative titles ("Fix grafana datasource
    # URL", "Add Splunk"), and dumping those into a catch-all bucket makes
    # the changelog markedly less useful. Only the leading word is examined,
    # so "Fix" classifies but "Prep for 1.2" stays uncategorised rather than
    # being guessed at.
    scope_stripped = _BRACKET_SCOPE.sub("", raw_title).strip()
    first_word = re.split(r"[\s:;,.]+", scope_stripped, maxsplit=1)[0].lower() if scope_stripped else ""
    section = VERB_HINTS.get(first_word)
    if section:
        return section, raw_title, "breaking change" in raw_title.lower(), ""

    return "other", raw_title, "breaking change" in raw_title.lower(), ""


def build_entries(
    prs: List[Dict], team_prefixes: Optional[List[str]] = None
) -> List[ChangelogEntry]:
    """Classify every PR, newest merge first, de-duplicated by PR number."""
    entries: Dict[int, ChangelogEntry] = {}
    for pr in prs:
        entry = classify_pull_request(pr, team_prefixes)
        if entry is not None:
            entries.setdefault(entry.number, entry)

    ordered = list(entries.values())
    ordered.sort(key=lambda e: (e.merged_at or "", e.number), reverse=True)
    return ordered


def group_entries(entries: List[ChangelogEntry]) -> Dict[str, List[ChangelogEntry]]:
    """
    Group entries into their sections, in SECTIONS display order.

    Breaking changes stay inside their own section rather than being hoisted
    into a separate one -- they are rendered with a marker instead, so a
    breaking feature still reads as a feature.
    """
    grouped: Dict[str, List[ChangelogEntry]] = {}
    for key, _heading in SECTIONS:
        matching = [e for e in entries if e.section == key]
        if matching:
            grouped[key] = matching
    return grouped


def extract_pr_numbers_from_commits(commits: List[Dict]) -> List[int]:
    """
    Pull PR numbers out of squash-merge commit messages ("... (#123)").

    This is how the compare-range path maps commits back to PRs without an
    API call per commit. Commits with no such suffix (true merge commits, or
    direct pushes) yield nothing and are resolved separately.
    """
    numbers: List[int] = []
    for commit in commits:
        message = str(((commit.get("commit") or {}) or {}).get("message") or "")
        first_line = message.split("\n", 1)[0]
        match = _PR_NUMBER_IN_MESSAGE.search(first_line)
        if match:
            number = int(match.group(1))
            if number not in numbers:
                numbers.append(number)
    return numbers


def render_markdown(
    version: str,
    grouped: Dict[str, List[ChangelogEntry]],
    owner: str,
    repo: str,
    previous_tag: str,
    current_tag: str,
    release_manager: str,
    generated_at: str,
) -> str:
    """Render the changelog as Markdown (used for Notion and console preview)."""
    total = sum(len(v) for v in grouped.values())
    lines = [
        f"# {version}",
        "",
        f"{total} merged pull request(s) in `{owner}/{repo}` "
        f"between `{previous_tag}` and `{current_tag}`.",
        "",
    ]

    breaking = [e for entries in grouped.values() for e in entries if e.breaking]
    if breaking:
        lines.append("## Breaking Changes")
        lines.append("")
        for entry in breaking:
            lines.append(f"- {_entry_line(entry)}")
        lines.append("")

    for key, heading in SECTIONS:
        entries = grouped.get(key)
        if not entries:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for entry in entries:
            lines.append(f"- {_entry_line(entry)}")
        lines.append("")

    if not total:
        lines.append("_No merged pull requests found in this range._")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Generated for {release_manager} on {generated_at} by the release changelog agent._"
    )
    return "\n".join(lines)


def _entry_line(entry: ChangelogEntry) -> str:
    """One Markdown bullet for a PR, with links back to GitHub and Linear."""
    parts = []
    if entry.breaking:
        parts.append("**BREAKING**")
    if entry.scope:
        parts.append(f"**{entry.scope}:**")
    parts.append(entry.title)

    suffix = [f"[#{entry.number}]({entry.url})" if entry.url else f"#{entry.number}"]
    for issue in entry.linear_issues:
        url = issue.get("url")
        identifier = issue.get("_identifier") or issue.get("identifier") or "Linear"
        suffix.append(f"[{identifier}]({url})" if url else identifier)
    if entry.author:
        suffix.append(f"@{entry.author}")

    return f"{' '.join(parts)} ({', '.join(suffix)})"


def render_confluence_storage(
    version: str,
    grouped: Dict[str, List[ChangelogEntry]],
    owner: str,
    repo: str,
    previous_tag: str,
    current_tag: str,
    release_manager: str,
    generated_at: str,
) -> str:
    """
    Render the changelog in Confluence "storage" format (XHTML-ish).

    Storage format is used rather than ADF because it is plain markup that can
    be generated and reviewed as text, whereas ADF would have to be built as a
    JSON tree and then serialized into a string parameter.
    """
    total = sum(len(v) for v in grouped.values())
    out = [
        f"<h1>{_esc(version)}</h1>",
        f"<p>{total} merged pull request(s) in <code>{_esc(owner)}/{_esc(repo)}</code> "
        f"between <code>{_esc(previous_tag)}</code> and <code>{_esc(current_tag)}</code>.</p>",
    ]

    breaking = [e for entries in grouped.values() for e in entries if e.breaking]
    if breaking:
        out.append("<h2>Breaking Changes</h2><ul>")
        out.extend(f"<li>{_entry_html(e)}</li>" for e in breaking)
        out.append("</ul>")

    for key, heading in SECTIONS:
        entries = grouped.get(key)
        if not entries:
            continue
        out.append(f"<h2>{_esc(heading)}</h2><ul>")
        out.extend(f"<li>{_entry_html(e)}</li>" for e in entries)
        out.append("</ul>")

    if not total:
        out.append("<p><em>No merged pull requests found in this range.</em></p>")

    out.append(
        f"<hr/><p><em>Generated for {_esc(release_manager)} on {_esc(generated_at)} "
        f"by the release changelog agent.</em></p>"
    )
    return "".join(out)


def _entry_html(entry: ChangelogEntry) -> str:
    """One Confluence storage-format list item for a PR."""
    parts = []
    if entry.breaking:
        parts.append("<strong>BREAKING</strong>")
    if entry.scope:
        parts.append(f"<strong>{_esc(entry.scope)}:</strong>")
    parts.append(_esc(entry.title))

    links = []
    if entry.url:
        links.append(f'<a href="{_esc(entry.url)}">#{entry.number}</a>')
    else:
        links.append(f"#{entry.number}")
    for issue in entry.linear_issues:
        url = issue.get("url")
        identifier = issue.get("_identifier") or issue.get("identifier") or "Linear"
        links.append(f'<a href="{_esc(url)}">{_esc(identifier)}</a>' if url else _esc(identifier))
    if entry.author:
        links.append(f"@{_esc(entry.author)}")

    return f"{' '.join(parts)} ({', '.join(links)})"


def render_notion_blocks(
    version: str,
    grouped: Dict[str, List[ChangelogEntry]],
    owner: str,
    repo: str,
    previous_tag: str,
    current_tag: str,
    release_manager: str,
    generated_at: str,
) -> List[Dict]:
    """
    Render the changelog as RAW Notion block objects.

    Raw blocks (not the simplified {type, text} shape) because changelog
    entries need inline links on the PR and Linear references, which the
    simplified shape cannot express.
    """
    total = sum(len(v) for v in grouped.values())
    blocks: List[Dict] = [
        _para([
            _text(f"{total} merged pull request(s) in "),
            _text(f"{owner}/{repo}", code=True),
            _text(f" between {previous_tag} and {current_tag}."),
        ])
    ]

    breaking = [e for entries in grouped.values() for e in entries if e.breaking]
    if breaking:
        blocks.append(_heading("Breaking Changes"))
        blocks.extend(_bullet(e) for e in breaking)

    for key, heading in SECTIONS:
        entries = grouped.get(key)
        if not entries:
            continue
        blocks.append(_heading(heading))
        blocks.extend(_bullet(e) for e in entries)

    if not total:
        blocks.append(_para([_text("No merged pull requests found in this range.", italic=True)]))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append(_para([
        _text(f"Generated for {release_manager} on {generated_at} "
              f"by the release changelog agent.", italic=True)
    ]))
    return blocks


def _text(content: str, link: str = None, code: bool = False, italic: bool = False) -> Dict:
    """One Notion rich-text run."""
    item: Dict = {"type": "text", "text": {"content": content[:2000]}}
    if link:
        item["text"]["link"] = {"url": link}
    annotations = {}
    if code:
        annotations["code"] = True
    if italic:
        annotations["italic"] = True
    if annotations:
        item["annotations"] = annotations
    return item


def _para(rich_text: List[Dict]) -> Dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _heading(text: str) -> Dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_text(text)]},
    }


def _bullet(entry: ChangelogEntry) -> Dict:
    """One Notion bulleted list item for a PR, with real inline links."""
    rich: List[Dict] = []
    if entry.breaking:
        rich.append({**_text("BREAKING "), "annotations": {"bold": True}})
    if entry.scope:
        rich.append({**_text(f"{entry.scope}: "), "annotations": {"bold": True}})
    rich.append(_text(entry.title))

    rich.append(_text(" ("))
    rich.append(_text(f"#{entry.number}", link=entry.url or None))
    for issue in entry.linear_issues:
        identifier = issue.get("_identifier") or issue.get("identifier") or "Linear"
        rich.append(_text(", "))
        rich.append(_text(identifier, link=issue.get("url") or None))
    if entry.author:
        rich.append(_text(f", @{entry.author}"))
    rich.append(_text(")"))

    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich},
    }


def _esc(value) -> str:
    """Escape text for XHTML storage format."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
