"""
Aggregation logic: merge Salesforce open Opportunities and HubSpot open Deals
into one pipeline-by-stage view, calculate coverage ratios, flag at-risk
segments, and draft rule-based commentary text.

Coverage ratio formula
----------------------
For a given scope (here: total open pipeline across both CRMs, this cycle):

    coverage_ratio = total_open_pipeline_value / quota_target

This is the standard "pipeline coverage" metric used in SaaS sales planning:
if your quota this period is $100k and you have $350k of genuinely open
pipeline, your coverage ratio is 3.5x. A commonly used rule of thumb is that
healthy coverage sits at 3x-4x quota (accounting for typical win rates in the
20-35% range) -- below that, there usually isn't enough pipeline in the
funnel to realistically hit the number even with a strong close rate. This
agent defaults COVERAGE_RATIO_TARGET to 3.0 and flags the overall forecast
as "at risk" when coverage falls below it. The target and the quota it is
measured against are both configurable (COVERAGE_RATIO_TARGET, QUOTA_TARGET)
because neither Salesforce nor HubSpot expose an authoritative "quota" object
through the tools available to this agent (HubSpot does have Goals/Forecasts
objects, but no goal-target read tool was available in the connected
HUBSPOT connector's toolset at build time -- see hubspot_goal_targets_list
which manages *user* goal targets, not a team quota figure).

At-risk segment flagging
-------------------------
A per-stage segment (grouping of open deals/opportunities sharing the same
stage label) is flagged "at risk" if ANY of the following hold, using only
signals actually present on the records returned by the two CRMs:

  1. Stale: the stage's average days-until-close is negative (CloseDate has
     already passed) -- these are open records whose close date slipped and
     were never updated, a classic forecast-inflation signal.
  2. Thin: the stage has fewer than 2 deals/opportunities AND is one of the
     last two stages before close (i.e. deep pipeline with too few records
     to be a reliable forecast contributor).
  3. Underweighted: the stage's share of total open value is under 5% of
     total pipeline while sitting in a "commit"-like late stage (heuristic:
     stage label containing "negotiation", "contract", "decision", or
     HubSpot's isClosed=false stages with probability >= 0.6) -- suggests
     the deal isn't actually being progressed at the rate its stage implies.

The overall forecast is flagged "at risk" if the total coverage_ratio is
below COVERAGE_RATIO_TARGET, independent of any single stage's flag.
"""

import datetime
import logging
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class StageSegment:
    """Aggregated open pipeline for a single stage label, across both CRMs."""

    def __init__(self, stage_label: str):
        self.stage_label = stage_label
        self.deal_count = 0
        self.total_value = 0.0
        self.close_date_deltas_days: List[int] = []  # negative = overdue
        self.sources: Dict[str, int] = defaultdict(int)  # "Salesforce" / "HubSpot" -> count

    def add(self, amount: float, close_date: Optional[datetime.date], source: str) -> None:
        self.deal_count += 1
        self.total_value += amount
        self.sources[source] += 1
        if close_date is not None:
            delta = (close_date - datetime.date.today()).days
            self.close_date_deltas_days.append(delta)

    def overdue_count(self) -> int:
        return sum(1 for d in self.close_date_deltas_days if d < 0)

    def avg_days_to_close(self) -> Optional[float]:
        if not self.close_date_deltas_days:
            return None
        return sum(self.close_date_deltas_days) / len(self.close_date_deltas_days)


_LATE_STAGE_KEYWORDS = ("negotiation", "contract", "decision", "review", "closing")


def _is_late_stage(stage_label: str) -> bool:
    lowered = stage_label.lower()
    return any(keyword in lowered for keyword in _LATE_STAGE_KEYWORDS)


def _parse_date(value) -> Optional[datetime.date]:
    """Parse a Salesforce/HubSpot date string (YYYY-MM-DD, optionally with a time part)."""
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def build_stage_segments(
    salesforce_records: List[Dict],
    hubspot_deals: List[Dict],
    hubspot_stage_labels: Dict[str, str],
) -> Dict[str, StageSegment]:
    """
    Group Salesforce Opportunities (by StageName) and HubSpot Deals (by
    dealstage, resolved to a human label via hubspot_stage_labels) into a
    single dict of stage_label -> StageSegment.
    """
    segments: Dict[str, StageSegment] = {}

    for record in salesforce_records:
        stage = record.get("StageName") or "Unknown Stage"
        amount = float(record.get("Amount") or 0.0)
        close_date = _parse_date(record.get("CloseDate"))
        segments.setdefault(stage, StageSegment(stage)).add(amount, close_date, "Salesforce")

    for deal in hubspot_deals:
        props = deal.get("properties", deal)
        stage_id = props.get("dealstage") or ""
        stage_label = hubspot_stage_labels.get(stage_id, stage_id or "Unknown Stage")
        amount = float(props.get("amount") or 0.0)
        close_date = _parse_date(props.get("closedate"))
        segments.setdefault(stage_label, StageSegment(stage_label)).add(amount, close_date, "HubSpot")

    return segments


def flag_at_risk_stages(segments: Dict[str, StageSegment], total_open_value: float) -> Dict[str, List[str]]:
    """Return {stage_label: [reason, ...]} for every stage with at least one at-risk signal."""
    flags: Dict[str, List[str]] = {}

    for label, segment in segments.items():
        reasons = []

        if segment.overdue_count() > 0:
            reasons.append(
                f"{segment.overdue_count()} of {segment.deal_count} deal(s) have a close date in the past"
            )

        is_late = _is_late_stage(label)
        if is_late and segment.deal_count < 2:
            reasons.append(f"only {segment.deal_count} deal(s) in a late stage -- thin coverage")

        if is_late and total_open_value > 0 and (segment.total_value / total_open_value) < 0.05:
            pct = (segment.total_value / total_open_value) * 100
            reasons.append(f"only {pct:.1f}% of total pipeline value sits in this late stage")

        if reasons:
            flags[label] = reasons

    return flags


def calculate_coverage_ratio(total_open_value: float, quota_target: float) -> float:
    """coverage_ratio = total_open_pipeline_value / quota_target. See module docstring."""
    if quota_target <= 0:
        return 0.0
    return round(total_open_value / quota_target, 2)


def draft_commentary(
    segments: Dict[str, StageSegment],
    at_risk_flags: Dict[str, List[str]],
    total_open_value: float,
    coverage_ratio: float,
    coverage_ratio_target: float,
    quota_target: float,
    forecast_period: str,
) -> str:
    """
    Rule-based commentary draft (deterministic, no LLM required). Mirrors the
    reference repo's fallback-summarizer philosophy: always produces a usable
    narrative from real data, with an optional LLM upgrade in summarizer.py.
    """
    lines = [f"*Revenue Forecast Commentary -- {forecast_period}*", ""]

    overall_status = "AT RISK" if coverage_ratio < coverage_ratio_target else "ON TRACK"
    lines.append(
        f"Total open pipeline: ${total_open_value:,.0f} against a ${quota_target:,.0f} quota "
        f"-> {coverage_ratio}x coverage (target {coverage_ratio_target}x) -- *{overall_status}*."
    )
    lines.append("")

    if not segments:
        lines.append("No open pipeline found in Salesforce or HubSpot for this cycle.")
        return "\n".join(lines)

    lines.append("*By stage:*")
    for label, segment in sorted(segments.items(), key=lambda kv: kv[1].total_value, reverse=True):
        source_str = ", ".join(f"{count} {src}" for src, count in segment.sources.items())
        flag_marker = " :warning:" if label in at_risk_flags else ""
        lines.append(
            f"- *{label}*: {segment.deal_count} open ({source_str}), "
            f"${segment.total_value:,.0f}{flag_marker}"
        )
    lines.append("")

    if at_risk_flags:
        lines.append("*At-risk segments:*")
        for label, reasons in at_risk_flags.items():
            for reason in reasons:
                lines.append(f"- *{label}*: {reason}")
    else:
        lines.append("No individual stage-level risk signals detected this cycle.")

    lines.append("")
    lines.append("_Generated by your Revenue Forecast Commentary Agent from live Salesforce + HubSpot pipeline data._")
    return "\n".join(lines)
