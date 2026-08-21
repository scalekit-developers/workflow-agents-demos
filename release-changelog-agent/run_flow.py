#!/usr/bin/env python3
"""
Release Changelog Agent: GitHub -> Linear -> Confluence + Notion

Resolves the commit range for a release, collects the pull requests merged in
it, groups them by feature / fix / chore, links each to its Linear issue, and
publishes the resulting changelog to a Confluence page and a Notion doc.

Scalekit Agent Auth handles OAuth for all four connectors -- token storage,
refresh, and every API call go through actions.execute_tool(). No manual token
management, no direct API imports.

Idempotency: a version is published at most once per repository per target.
The local state file is the fast path; Confluence additionally gets a remote
title search, so deleting state costs a search rather than producing a second
page. Notion titles are not unique, so its guard is the local record -- see
the README.

Setup:
  cp .env.example .env        # fill in your credentials
  pip install -r requirements.txt
  python run_flow.py           # generate and publish one changelog
  python run_flow.py --dry-run # render and print it, publish nothing

Exit codes:
  0   = success (changelog generated; published where enabled)
  1   = error (config missing, provisioning failed, or GitHub unreachable)
  2   = no data (no merged pull requests in the resolved range)
  130 = interrupted (Ctrl+C or SIGTERM)
"""

import datetime
import signal
import sys
from typing import Dict, List, Optional

import scalekit.client
from dotenv import load_dotenv

import config as config_module
from config import Config
from connectors import (
    ConfluenceConnector,
    ConnectorError,
    GitHubConnector,
    LinearConnector,
    NotionConnector,
)
import logging_config
from changelog import (
    build_entries,
    extract_pr_numbers_from_commits,
    group_entries,
    render_confluence_storage,
    render_markdown,
    render_notion_blocks,
)
from provisioning import ProvisioningError, resolve_confluence_space, resolve_release_range
from state import StateManager

load_dotenv()
logger = logging_config.setup_logging(__name__)
config_module.logger = logger

# Notion rejects a create/append carrying more than 100 children at once.
NOTION_BLOCK_LIMIT = 100

_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.warning("Received signal, shutting down gracefully...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def init_config() -> Config:
    cfg = Config()
    if "--dry-run" in sys.argv:
        cfg.dry_run = True
    cfg.validate()
    return cfg


def init_scalekit(cfg: Config):
    try:
        sk = scalekit.client.ScalekitClient(
            client_id=cfg.scalekit_client_id,
            client_secret=cfg.scalekit_client_secret,
            env_url=cfg.scalekit_env_url,
        )
        logger.debug("Scalekit client initialized")
        return sk
    except Exception as e:
        logger.error(f"Failed to initialize Scalekit: {e}", exc_info=True)
        sys.exit(1)


def collect_merged_prs(
    github: GitHubConnector, cfg: Config, previous_tag: str, current_tag: str
) -> List[Dict]:
    """
    Collect the pull requests merged between two tags.

    Strategy, in order:
      1. Compare the two tags and read PR numbers out of squash-merge commit
         messages ("... (#123)"). This is exact -- it reflects what is
         actually in the range, not what merged around the same time.
      2. For commits with no such suffix (real merge commits, direct pushes),
         ask GitHub which PR each commit belongs to.
      3. If the compare fails entirely, fall back to recently-merged PRs and
         filter by the current tag's date, which is approximate but keeps the
         agent useful on repos where compare is unavailable.
    """
    numbers: List[int] = []
    unresolved: List[str] = []

    try:
        comparison = github.compare(cfg.github_owner, cfg.github_repo, previous_tag, current_tag)
        commits = comparison.get("commits") or []
        logger.info(f"  Compared {previous_tag}...{current_tag}: {len(commits)} commit(s)")

        numbers = extract_pr_numbers_from_commits(commits)
        matched_shas = set()
        for commit in commits:
            message = str((commit.get("commit") or {}).get("message") or "").split("\n", 1)[0]
            if "(#" not in message:
                sha = commit.get("sha")
                if sha:
                    unresolved.append(str(sha))
            else:
                matched_shas.add(commit.get("sha"))
    except ConnectorError as e:
        logger.warning(
            f"  Could not compare {previous_tag}...{current_tag} ({e}) -- "
            f"falling back to recently merged pull requests"
        )
        return github.list_merged_pull_requests(
            cfg.github_owner, cfg.github_repo, per_page=cfg.max_prs
        )

    # Resolve the stragglers, but cap the work: a range with hundreds of merge
    # commits should not fire hundreds of extra API calls.
    lookup_budget = 25
    for sha in unresolved[:lookup_budget]:
        if _shutdown_requested:
            break
        try:
            for pr in github.pull_requests_for_commit(cfg.github_owner, cfg.github_repo, sha):
                number = pr.get("number")
                if number is not None and int(number) not in numbers:
                    numbers.append(int(number))
        except ConnectorError:
            continue
    if len(unresolved) > lookup_budget:
        logger.warning(
            f"  {len(unresolved) - lookup_budget} commit(s) without a PR reference were not "
            f"resolved (lookup budget {lookup_budget}); they are omitted from the changelog."
        )

    if not numbers:
        return []

    # Fetch full PR payloads. The list endpoint carries labels and head refs
    # that the per-commit endpoint's summaries can lack, so PRs are hydrated
    # from one list call and only individually fetched when missing.
    logger.info(f"  Resolved {len(numbers)} pull request number(s) in range")
    listed = {
        int(pr["number"]): pr
        for pr in github.list_merged_pull_requests(
            cfg.github_owner, cfg.github_repo, per_page=cfg.max_prs
        )
        if pr.get("number") is not None
    }

    resolved: List[Dict] = []
    for number in numbers:
        if number in listed:
            resolved.append(listed[number])
            continue
        try:
            pr = github.execute_tool(
                "github_pull_request_get",
                owner=cfg.github_owner, repo=cfg.github_repo, pull_number=number,
            )
            if pr and pr.get("merged_at"):
                resolved.append(pr)
        except ConnectorError:
            logger.debug(f"  Could not fetch PR #{number}")

    return resolved


def enrich_with_linear(linear: LinearConnector, entries, cfg: Config) -> int:
    """
    Attach Linear issue details to entries that reference an identifier.

    Returns the number of issues successfully resolved. A miss is normal --
    identifiers get typo'd, issues get deleted, and other trackers use the
    same ABC-123 shape -- so a failed lookup drops the link rather than the
    changelog entry.
    """
    resolved = 0
    seen: Dict[str, Optional[Dict]] = {}

    for entry in entries:
        if _shutdown_requested:
            break
        for identifier in entry.linear_ids:
            if identifier not in seen:
                issue = linear.get_issue(identifier)
                if issue is not None:
                    # linear_issue_get's selection set omits `identifier`, so
                    # the one we searched with is preserved for rendering.
                    issue = dict(issue)
                    issue["_identifier"] = identifier
                seen[identifier] = issue
            issue = seen[identifier]
            if issue:
                entry.linear_issues.append(issue)
                resolved += 1

    return resolved


def publish_confluence(
    confluence: ConfluenceConnector, cfg: Config, state: StateManager,
    space_id: str, title: str, body_html: str,
) -> Optional[str]:
    """Publish to Confluence unless this version already has a page."""
    cached = (state.get_published(cfg.github_owner, cfg.github_repo, cfg.release_version) or {}).get("confluence")
    if cached:
        logger.info(f"  Already published to Confluence: {cached} -- skipping")
        return cached

    existing = confluence.find_page_by_title(cfg.confluence_space_key, title)
    if existing:
        url = _confluence_url(existing)
        logger.info(f"  A Confluence page titled '{title}' already exists ({url}) -- skipping")
        state.mark_published(cfg.github_owner, cfg.github_repo, cfg.release_version, "confluence", url)
        return url

    try:
        result = confluence.create_page(
            space_id=space_id, title=title, body_html=body_html,
            parent_id=cfg.confluence_parent_id or None,
        )
    except ConnectorError as e:
        logger.error(f"  Failed to publish to Confluence: {e}")
        return None

    url = _confluence_url(result)
    state.mark_published(cfg.github_owner, cfg.github_repo, cfg.release_version, "confluence", url)
    logger.info(f"  [OK] Published to Confluence: {url}")
    return url


def publish_notion(
    notion: NotionConnector, cfg: Config, state: StateManager,
    title: str, blocks: List[Dict],
) -> Optional[str]:
    """
    Publish to Notion unless this version was already published from here.

    Notion page titles are not unique, so unlike Confluence there is no
    reliable remote "does this already exist" check -- the local state record
    is the guard, and it is documented as such.
    """
    cached = (state.get_published(cfg.github_owner, cfg.github_repo, cfg.release_version) or {}).get("notion")
    if cached:
        logger.info(f"  Already published to Notion: {cached} -- skipping")
        return cached

    head, tail = blocks[:NOTION_BLOCK_LIMIT], blocks[NOTION_BLOCK_LIMIT:]
    try:
        result = notion.create_page(cfg.notion_parent_page_id, title, head)
    except ConnectorError as e:
        logger.error(f"  Failed to publish to Notion: {e}")
        return None

    page_id = result.get("id")
    url = result.get("url") or (f"https://notion.so/{str(page_id).replace('-', '')}" if page_id else "")

    # Notion caps children per request, so a long changelog is appended in
    # chunks after the page exists.
    while tail and page_id:
        chunk, tail = tail[:NOTION_BLOCK_LIMIT], tail[NOTION_BLOCK_LIMIT:]
        try:
            notion.append_blocks(str(page_id), chunk)
        except ConnectorError as e:
            logger.warning(f"  Published page but could not append all content: {e}")
            break

    if url:
        state.mark_published(cfg.github_owner, cfg.github_repo, cfg.release_version, "notion", url)
        logger.info(f"  [OK] Published to Notion: {url}")
    return url or None


def run_cycle(cfg: Config, actions, state: StateManager) -> Optional[int]:
    """
    Generate and publish one changelog.

    Returns the number of changelog entries, or None when the range held no
    merged pull requests at all.
    """
    github = GitHubConnector(actions, cfg.github_user, cfg.github_connector)
    repo_label = f"{cfg.github_owner}/{cfg.github_repo}"

    logger.info(f"Step 1: Resolving release range for {repo_label}")
    try:
        previous_tag, current_tag = resolve_release_range(
            github, cfg.github_owner, cfg.github_repo, cfg.previous_tag, cfg.current_tag
        )
    except ProvisioningError as e:
        logger.error(str(e))
        return None

    if not cfg.release_version:
        cfg.release_version = current_tag
        logger.info(f"  RELEASE_VERSION not set -- using the current tag '{current_tag}'")

    logger.info("Step 2: Collecting merged pull requests")
    prs = collect_merged_prs(github, cfg, previous_tag, current_tag)
    if not prs:
        logger.warning(f"No merged pull requests found between {previous_tag} and {current_tag}")
        return None

    entries = build_entries(prs, cfg.linear_team_prefixes)
    logger.info(f"  {len(entries)} merged pull request(s) in this release")

    logger.info("Step 3: Grouping changes and linking Linear issues")
    grouped = group_entries(entries)
    breakdown = ", ".join(f"{len(v)} {k}" for k, v in grouped.items())
    logger.info(f"  Grouped: {breakdown}")

    referenced = sum(1 for e in entries if e.linear_ids)
    if cfg.enable_linear and referenced:
        linear = LinearConnector(actions, cfg.linear_user, cfg.linear_connector)
        resolved = enrich_with_linear(linear, entries, cfg)
        logger.info(f"  Linked {resolved}/{referenced} Linear issue reference(s)")
    elif cfg.enable_linear:
        logger.info("  No Linear issue identifiers found in these pull requests")
    else:
        logger.info("  Linear enrichment disabled (ENABLE_LINEAR=false)")

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    render_args = dict(
        version=cfg.release_version, grouped=grouped,
        owner=cfg.github_owner, repo=cfg.github_repo,
        previous_tag=previous_tag, current_tag=current_tag,
        release_manager=cfg.release_manager, generated_at=generated_at,
    )
    title = f"{cfg.github_repo} {cfg.release_version} release notes"

    logger.info("Step 4: Publishing changelog")
    if cfg.dry_run:
        logger.info(f"  [DRY RUN] would publish '{title}'")
        print()
        print(render_markdown(**render_args))
        print()
        return len(entries)

    if cfg.publish_confluence:
        confluence = ConfluenceConnector(actions, cfg.confluence_user, cfg.confluence_connector)
        try:
            space_id = resolve_confluence_space(confluence, cfg.confluence_space_key)
            publish_confluence(
                confluence, cfg, state, space_id, title,
                render_confluence_storage(**render_args),
            )
        except ProvisioningError as e:
            logger.error(f"  {e}")
    else:
        logger.info("  Confluence publishing disabled (PUBLISH_CONFLUENCE=false)")

    if cfg.publish_notion:
        notion = NotionConnector(actions, cfg.notion_user, cfg.notion_connector)
        publish_notion(notion, cfg, state, title, render_notion_blocks(**render_args))
    else:
        logger.info("  Notion publishing disabled (PUBLISH_NOTION=false)")

    return len(entries)


def main() -> int:
    cfg = init_config()
    sk = init_scalekit(cfg)
    actions = sk.actions
    state = StateManager()

    if cfg.dry_run:
        logger.warning("DRY RUN -- the changelog will be rendered but not published")

    logger.info("Step 0: Checking connector auth")
    all_active = True
    for connector_name, identifier in cfg.get_connector_users().items():
        conn = _connector_for_check(actions, connector_name, identifier)
        if not conn.check_auth():
            all_active = False

    if not all_active:
        logger.warning("Some connectors are not authorized. Proceeding anyway -- affected steps will be skipped.")

    count = run_cycle(cfg, actions, state)
    if count is None:
        return 2
    logger.info(f"[OK] Changelog covers {count} pull request(s)")
    return 0


def _confluence_url(page: Dict) -> str:
    """Best-effort browser URL for a created/found Confluence page."""
    links = page.get("_links") if isinstance(page.get("_links"), dict) else {}
    base = str(links.get("base") or "").rstrip("/")
    webui = str(links.get("webui") or "")
    if base and webui:
        return f"{base}{webui}"
    if webui:
        return webui
    page_id = page.get("id")
    return f"(page id {page_id})" if page_id else "(created)"


def _connector_for_check(actions, connector_name: str, identifier: str):
    """Lightweight wrapper just for the Step 0 auth-check loop."""
    from connectors import Connector
    return Connector(actions, connector_name, identifier)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (signal)")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
