"""
Aggregation logic: turn a list of raw Gong calls (each already known to
mention at least one tracked competitor) into one briefing digest per sales
rep, covering every call and every competitor that rep was involved with in
this cycle.

Mention detection
------------------
A "competitor mention" on a call is detected two ways, in priority order:

  1. Tracker hits: if get_calls_extensive() returns a content.trackers list
     for the call (Gong's tracker-hit signal, see connectors.py module
     docstring) and a tracker's name matches a configured competitor name
     via _word_matches (case-insensitive whole-word match, e.g. tracker
     "Salesforce mention" matches competitor "Salesforce" but "Sales" does
     not), that's a mention.
  2. Transcript text fallback: if a call has no matching tracker hit (either
     because no Gong tracker exists for that competitor, or tracker data
     wasn't returned), the call's transcript text is scanned for the
     competitor name as a case-insensitive whole-word match.

A single call can mention multiple competitors, and this shows up as
multiple entries in that rep's digest for the one call (see
build_rep_digests below), not multiple separate DMs.

Rep identification
--------------------
Scalekit's gong_calls_get tool has no way to request Gong's "parties"
(participant) data -- its input schema only accepts call_ids and
workspace_id, confirmed by reading the tool's own jsonnet_template. The
real, working path is each call's metaData.primaryUserId, resolved in one
batch via gong_users_get to a name/email (see identify_rep and
GongConnector.build_user_lookup). A call whose primaryUserId cannot be
resolved is skipped with a warning (see build_rep_digests) rather than
guessed at, since DMing the wrong person is worse than skipping one call.
"""

import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CallMention:
    """One (call, competitor) mention, with everything needed to render it in a digest line."""

    def __init__(
        self,
        call_id: str,
        call_url: str,
        call_title: str,
        call_date: str,
        competitor: str,
        mention_start_ms: Optional[int] = None,
    ):
        self.call_id = call_id
        self.call_url = call_url
        self.call_title = call_title
        self.call_date = call_date
        self.competitor = competitor
        # Milliseconds into the call where the mention was found, from the
        # transcript sentence's "start" field. None for tracker-detected
        # mentions (Gong's tracker-hit signal doesn't carry a timestamp) or
        # when the matching transcript sentence had no "start" field.
        self.mention_start_ms = mention_start_ms


class RepDigest:
    """All of one rep's mentions for this briefing cycle, plus their resolved Slack identity."""

    def __init__(self, rep_identifier: str, rep_display_name: str):
        self.rep_identifier = rep_identifier  # email if known, else display name
        self.rep_display_name = rep_display_name
        self.mentions: List[CallMention] = []

    def add(self, mention: CallMention) -> None:
        self.mentions.append(mention)

    def competitors(self) -> List[str]:
        """Unique competitor names mentioned across this rep's calls, in first-seen order."""
        seen = []
        for m in self.mentions:
            if m.competitor not in seen:
                seen.append(m.competitor)
        return seen


def _word_matches(text: str, needle: str) -> bool:
    """Case-insensitive whole-word/phrase match, avoiding substring false positives (e.g. "Sales" != "Salesforce")."""
    if not text or not needle:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(needle.strip()) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def detect_mentions_from_trackers(call_detail: Dict, competitor_names: List[str]) -> List[str]:
    """
    Return the subset of competitor_names whose tracker fired on this call,
    per Gong's content.trackers (or top-level trackers) field. A tracker
    "counts" toward a competitor if the tracker's name contains the
    competitor's name as a whole word/phrase (case-insensitive), since a
    workspace's tracker might be named "Salesforce mention" or just
    "Salesforce", not necessarily an exact match to the competitor string.
    """
    trackers = (
        (call_detail.get("content") or {}).get("trackers")
        or call_detail.get("trackers")
        or []
    )
    if not trackers:
        return []

    hit_names = []
    for tracker in trackers:
        name = tracker.get("name") or ""
        count = tracker.get("count", tracker.get("occurrenceCount", 1))
        if not name or not count:
            continue
        for competitor in competitor_names:
            if _word_matches(name, competitor) and competitor not in hit_names:
                hit_names.append(competitor)
    return hit_names


def detect_mentions_from_transcript(
    transcript_sentences: List[Dict], competitor_names: List[str]
) -> Dict[str, Optional[int]]:
    """
    Fallback mention detector: scan transcript sentences for each competitor
    name as a whole-word match, sentence by sentence (not one joined blob),
    so the "start" timestamp of the first sentence that mentions each
    competitor can be captured. transcript_sentences is Gong's
    speaker-attributed sentence list for one call (flattened across speakers
    by the caller); each item is expected to carry "text" and "start"
    (milliseconds into the call, verified live against a real transcript
    response) fields.

    Returns {competitor: first_mention_start_ms}, where first_mention_start_ms
    is None if a "start" field wasn't present on the matching sentence (kept
    as a mention either way -- a missing timestamp just means the digest line
    won't include a "(at MM:SS)" hint for that one).
    """
    if not transcript_sentences:
        return {}

    hits: Dict[str, Optional[int]] = {}
    for sentence in transcript_sentences:
        if len(hits) >= len(competitor_names):
            break
        if not isinstance(sentence, dict):
            continue
        text = sentence.get("text", "")
        if not text:
            continue
        for competitor in competitor_names:
            if competitor in hits:
                continue
            if _word_matches(text, competitor):
                start = sentence.get("start")
                hits[competitor] = int(start) if isinstance(start, (int, float)) else None

    return hits


def _format_timestamp(start_ms: Optional[int]) -> str:
    """Render a millisecond offset as MM:SS for a human-readable 'jump to' hint. Empty string if unknown."""
    if start_ms is None or start_ms < 0:
        return ""
    total_seconds = start_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def identify_rep(
    call_detail: Dict,
    pmm_email_domain: str,
    user_lookup: Optional[Dict[str, Dict]] = None,
) -> Optional[Dict]:
    """
    Identify the internal sales rep on a call.

    Scalekit's gong_calls_get tool does not expose Gong's contentSelector
    (verified live against the tool's own published input_schema: only
    call_ids and workspace_id are accepted -- there is no way to request
    parties/participant data through this tool at all). So a call's
    "parties" list is never actually populated in practice, even though the
    field is checked below for forward-compatibility in case Scalekit adds
    that parameter later.

    The real, verified fallback: gong_calls_list/get_calls_extensive
    reliably return metaData.primaryUserId. Callers resolve the distinct set
    of primaryUserIds once per run via GongConnector.get_users() and pass
    the result here as user_lookup ({user_id: {"emailAddress":..,"firstName":..,
    "lastName":..}}), verified live to return real name/email data.

    Returns {"identifier": <email or name>, "display_name": <name>} or None
    if no internal participant can be identified by any path -- callers
    must skip the call with a warning rather than guess.
    """
    top_level_parties = call_detail.get("parties")
    if top_level_parties:
        parties = top_level_parties
    else:
        content = call_detail.get("content")
        parties = content.get("parties") or [] if isinstance(content, dict) else []

    candidates = [p for p in parties if isinstance(p, dict) and p.get("affiliation") == "Internal"]

    if not candidates and pmm_email_domain:
        candidates = [
            p for p in parties
            if isinstance(p, dict)
            and "@" in (p.get("emailAddress") or "")
            and (p.get("emailAddress") or "").split("@")[-1].lower() == pmm_email_domain.lower()
        ]

    if candidates:
        rep = candidates[0]
        email = rep.get("emailAddress") or ""
        name = rep.get("name") or email or "Unknown rep"
        return {"identifier": email or name, "display_name": name}

    # Real fallback path: primaryUserId -> gong_users_get, since parties is
    # never populated by the tool as Scalekit has wrapped it (see docstring).
    if user_lookup:
        meta = call_detail.get("metaData") or call_detail.get("meta") or call_detail
        primary_user_id = meta.get("primaryUserId")
        user = user_lookup.get(str(primary_user_id)) if primary_user_id else None
        if user:
            email = user.get("emailAddress") or ""
            first = user.get("firstName") or ""
            last = user.get("lastName") or ""
            name = (f"{first} {last}".strip()) or email or "Unknown rep"
            return {"identifier": email or name, "display_name": name}

    return None


def build_rep_digests(
    calls_with_details: List[Dict],
    competitor_names: List[str],
    pmm_email_domain: str,
    call_transcripts: Optional[Dict[str, List[Dict]]] = None,
    user_lookup: Optional[Dict[str, Dict]] = None,
) -> Dict[str, RepDigest]:
    """
    Turn a list of enriched call-detail dicts (from GongConnector.
    get_calls_extensive) into {rep_identifier: RepDigest}, one entry per
    distinct rep, each holding every (call, competitor) mention found for
    that rep this cycle.

    call_transcripts, if provided, maps call_id -> flattened sentence list,
    used as the fallback detector when tracker hits find nothing for a call.
    Malformed call records (missing call ID, no identifiable rep, or no
    detected competitor mention at all) are skipped with a warning and do
    not abort processing of the remaining calls.
    """
    digests: Dict[str, RepDigest] = {}
    call_transcripts = call_transcripts or {}

    for call_detail in calls_with_details:
        meta = call_detail.get("metaData") or call_detail.get("meta") or call_detail
        call_id = meta.get("id") or call_detail.get("id")
        if not call_id:
            logger.warning("Skipping a Gong call record with no call ID (malformed response)")
            continue

        call_title = meta.get("title") or "Untitled call"
        call_url = meta.get("url") or ""
        call_date = meta.get("started") or meta.get("scheduled") or ""

        rep = identify_rep(call_detail, pmm_email_domain, user_lookup)
        if not rep:
            logger.warning(f"Skipping call {call_id} ('{call_title}'): no internal rep could be identified")
            continue

        tracker_matches = detect_mentions_from_trackers(call_detail, competitor_names)
        mention_timestamps: Dict[str, Optional[int]] = {c: None for c in tracker_matches}

        if not tracker_matches:
            transcript_hits = detect_mentions_from_transcript(
                call_transcripts.get(str(call_id), []), competitor_names
            )
            mention_timestamps = transcript_hits

        if not mention_timestamps:
            logger.debug(f"Call {call_id} ('{call_title}') has no detected competitor mention, skipping")
            continue

        digest = digests.setdefault(rep["identifier"], RepDigest(rep["identifier"], rep["display_name"]))
        for competitor, start_ms in mention_timestamps.items():
            digest.add(CallMention(call_id, call_url, call_title, call_date, competitor, start_ms))

    return digests


def render_digest_text(digest: RepDigest, battlecard_links: Dict[str, Optional[str]]) -> str:
    """
    Build the Slack DM body for one rep's digest: every call + competitor
    mention, grouped by competitor, each with a battlecard link if one was
    found (see NotionConnector.find_battlecard_page) or an explicit note
    when none was found -- never silently omitted.
    """
    lines = [f"*Competitive Intelligence Briefing for {digest.rep_display_name}*", ""]

    by_competitor: Dict[str, List[CallMention]] = defaultdict(list)
    for mention in digest.mentions:
        by_competitor[mention.competitor].append(mention)

    for competitor in digest.competitors():
        mentions = by_competitor[competitor]
        battlecard_url = battlecard_links.get(competitor)

        lines.append(f"*{competitor}* mentioned in {len(mentions)} call(s):")
        for m in mentions:
            date_str = m.call_date[:10] if m.call_date else "unknown date"
            call_line = f"  - {date_str}: {m.call_title}"
            if m.call_url:
                call_line += f" (<{m.call_url}|listen>)"
            # Real per-sentence timestamp when the mention came from the
            # transcript scan (see detect_mentions_from_transcript). Rendered
            # as human-readable "jump to" guidance rather than a query-param
            # deep link, since Gong's exact timestamp-linking URL contract
            # could not be verified live in this workspace -- a wrong guessed
            # param would silently land the rep at the wrong moment or fail,
            # which is worse than an honest text hint.
            timestamp = _format_timestamp(m.mention_start_ms)
            if timestamp:
                call_line += f" -- mention at {timestamp}"
            lines.append(call_line)

        if battlecard_url:
            lines.append(f"  Battlecard: {battlecard_url}")
        else:
            lines.append(f"  No {competitor} battlecard found in Notion yet -- consider creating one.")
        lines.append("")

    lines.append("_Generated by your Competitive Intelligence Briefing Agent from live Gong call data._")
    return "\n".join(lines)
