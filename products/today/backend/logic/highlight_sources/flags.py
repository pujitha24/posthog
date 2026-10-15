import re

from django.core.cache import cache
from django.utils import timezone

from products.feature_flags.backend.facade.usage import (
    FLAG_CALL_DAYS,
    CreatedFlag,
    FlagCalls,
    FlagCallSummary,
    list_flags_created_by,
    summarize_flag_calls,
)

from ...facade.enums import HighlightLabel
from ..highlight_rules import exact, human
from ..highlight_types import Candidate, Context, Found, Source

REACH_DROP = "Flag ‘{flag}’ reached {pct}% fewer users in the last 7 days"
REACH_DROP_DETAIL = "{before} → {now} · Check its release conditions"
UNUSED_ONE = "Flag ‘{flag}’ had no evaluations in the last 14 days"
UNUSED_ONE_DETAIL = "Delete it if no code uses it"
SAME_ONE = "Flag ‘{flag}’ is {value} for everyone"
SAME_ONE_DETAIL = "Remove it from the code, then delete it"
CLEANUP_MANY = "{n} of your flags are unused or the same for everyone"
CLEANUP_OLDEST_DETAIL = "Oldest: {names}"
CLEANUP_COST = "{n} of your flags were called {cost} times in the last 7 days"
CLEANUP_COST_DETAIL = "Same value for everyone: {names}"
AND_MORE = "{names} and {n} more"

EXCLUDED_FLAGS = re.compile(r"^(survey-targeting-|product-tour-targeting-)|:deleted")
RISKY_FLAGS = re.compile(r"(?i)(kill|override|switch|disable|maintenance|remote-config)|-[0-9a-f]{8,}$")
MIN_REACH = 20
SAME_VALUE_MIN_AGE_DAYS = 30
CLEANUP_COST_MIN = 100_000
DAY_SECONDS = 24 * 60 * 60
NO_CALLS = FlagCalls(
    people_this_week=0, people_last_week=0, calls_this_week=0, calls=0, values_this_week=0, value_this_week=None
)


def find(context: Context) -> Found:
    flags = [
        flag
        for flag in list_flags_created_by(context.team.id, context.user.id, limit=500)
        if not flag.remote_configuration and not EXCLUDED_FLAGS.search(flag.key)
    ]
    if not flags:
        return Found()
    summary = _daily_summary(context)
    reach_drops: list[Candidate] = []
    unused: list[tuple[CreatedFlag, int]] = []
    same: list[tuple[CreatedFlag, int, FlagCalls]] = []
    for flag in flags:
        calls = summary.by_key.get(flag.key, NO_CALLS)
        if calls.people_last_week >= MIN_REACH and calls.people_this_week <= calls.people_last_week * 0.5:
            reach_drops.append(_reach_drop(flag, calls))
        if flag.removal_blocked or RISKY_FLAGS.search(flag.key):
            continue
        age = (context.today - timezone.localtime(flag.created_at, context.team.timezone_info).date()).days
        if age >= SAME_VALUE_MIN_AGE_DAYS and calls.people_this_week >= MIN_REACH and calls.values_this_week == 1:
            same.append((flag, age, calls))
        elif age >= FLAG_CALL_DAYS and calls.calls == 0 and summary.complete:
            unused.append((flag, age))
    cleanup = _cleanup(context, unused, same)
    return Found(items=(*reach_drops, *([cleanup] if cleanup else [])))


def _daily_summary(context: Context) -> FlagCallSummary:
    key = f"today_highlights:flag_calls:v1:{context.team.id}:{context.restrictions}:{context.today.isoformat()}"
    cached = cache.get(key)
    if isinstance(cached, FlagCallSummary):
        return cached
    summary = summarize_flag_calls(context.team, context.user)
    cache.set(key, summary, DAY_SECONDS)
    return summary


def _reach_drop(flag: CreatedFlag, calls: FlagCalls) -> Candidate:
    return Candidate(
        key=f"flag_reach_drop:{flag.id}",
        label=HighlightLabel.RISK,
        family="flags",
        importance=0.6,
        relevance=1.0,
        title=REACH_DROP.format(
            flag=flag.key, pct=f"{abs(calls.people_this_week / calls.people_last_week - 1) * 100:.0f}"
        ),
        detail=REACH_DROP_DETAIL.format(now=exact(calls.people_this_week), before=exact(calls.people_last_week)),
        url=f"/feature_flags/{flag.id}",
    )


def _listing(keys: list[str]) -> str:
    names = ", ".join(keys[:3])
    return AND_MORE.format(names=names, n=len(keys) - 3) if len(keys) > 3 else names


def _cleanup(
    context: Context, unused: list[tuple[CreatedFlag, int]], same: list[tuple[CreatedFlag, int, FlagCalls]]
) -> Candidate | None:
    if context.today.weekday() == 0 and (unused or same):
        oldest = [
            flag.key for flag, _ in sorted([*unused, *((flag, age) for flag, age, _ in same)], key=lambda f: -f[1])
        ]
        if len(oldest) == 1 and unused:
            title, detail = UNUSED_ONE.format(flag=oldest[0]), UNUSED_ONE_DETAIL
        elif len(oldest) == 1:
            title, detail = (
                SAME_ONE.format(flag=oldest[0], value=same[0][2].value_this_week or "false"),
                SAME_ONE_DETAIL,
            )
        else:
            title = CLEANUP_MANY.format(n=len(oldest))
            detail = CLEANUP_OLDEST_DETAIL.format(names=", ".join(oldest[:3])) if len(oldest) > 5 else _listing(oldest)
    else:
        costly = [(flag, calls) for flag, _, calls in same if calls.calls_this_week > 0]
        cost = sum(calls.calls_this_week for _, calls in costly)
        if cost < CLEANUP_COST_MIN:
            return None
        title = CLEANUP_COST.format(n=len(costly), cost=human(cost))
        detail = CLEANUP_COST_DETAIL.format(names=_listing([flag.key for flag, _ in costly]))
    return Candidate(
        key="flag_cleanup",
        label=HighlightLabel.OPPORTUNITY,
        family="flag_cleanup",
        importance=0.3,
        relevance=1.0,
        title=title,
        detail=detail,
        url="/feature_flags",
    )


SOURCE = Source(name="flags", find=find, scopes=("feature_flag:read",))
