"""Approval gate — blocks sending an offer until a hiring manager reacts in Slack.

Polls slack_get_reactions (via SlackMCPConnector, the only Slack connector
that can read anything back) until the approve or reject emoji appears, or
the timeout elapses.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("offer-letter-agent")


@dataclass
class ApprovalResult:
    approved: bool
    timed_out: bool
    reacted_with: list


def wait_for_approval(
    get_reactions: Callable[[], list],
    approve_emoji: str,
    reject_emoji: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> ApprovalResult:
    """Poll get_reactions() until approve_emoji or reject_emoji appears, or timeout.

    `sleep` and `now` are injectable so tests can simulate elapsed time
    without actually waiting — do not call time.sleep/time.monotonic directly
    elsewhere in this function.
    """
    deadline = now() + timeout_seconds
    elapsed_polls = 0

    while now() < deadline:
        emojis = get_reactions()
        if reject_emoji in emojis:
            logger.warning(f"Offer rejected (reaction: :{reject_emoji}:)")
            return ApprovalResult(approved=False, timed_out=False, reacted_with=emojis)
        if approve_emoji in emojis:
            logger.info(f"Offer approved (reaction: :{approve_emoji}:)")
            return ApprovalResult(approved=True, timed_out=False, reacted_with=emojis)

        elapsed_polls += 1
        remaining = int(deadline - now())
        logger.debug(f"No decision yet, {remaining}s remaining (poll #{elapsed_polls})")
        sleep(min(poll_interval_seconds, max(remaining, 0)))

    logger.warning(f"Approval timed out after {timeout_seconds}s with no reaction")
    return ApprovalResult(approved=False, timed_out=True, reacted_with=[])
