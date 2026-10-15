from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.query import execute_hogql_query

from posthog.models.team import Team
from posthog.models.user import User

from products.feature_flags.backend.cleanup_blockers import excluded_flag_ids
from products.feature_flags.backend.facade.enums import FlagEvaluationsMode
from products.feature_flags.backend.facade.flags import get_flag_evaluations_read_mode
from products.feature_flags.backend.models.feature_flag import FeatureFlag

FLAG_CALL_DAYS = 14
FLAG_CALL_LIMIT = 10000
QUERY_TIMEOUT_SECONDS = 10
WINDOW = f"timestamp >= toStartOfDay(now()) - INTERVAL {FLAG_CALL_DAYS} DAY AND timestamp < toStartOfDay(now())"
THIS_WEEK = "timestamp >= toStartOfDay(now()) - INTERVAL 7 DAY"
COLUMNS = f"""
       uniqIf(person_id, {THIS_WEEK}) AS people_this_week,
       uniqIf(person_id, NOT ({THIS_WEEK})) AS people_last_week,
       countIf({THIS_WEEK}) AS calls_this_week,
       count() AS calls,
       uniqIf(toString({{response}}), {THIS_WEEK}) AS values_this_week,
       anyIf(toString({{response}}), {THIS_WEEK}) AS value_this_week"""
CALLS_FROM_EVENTS = f"""
SELECT properties.$feature_flag AS flag_key, {COLUMNS.format(response="properties.$feature_flag_response")}
FROM events
WHERE event = '$feature_flag_called' AND {WINDOW}
GROUP BY flag_key
LIMIT {FLAG_CALL_LIMIT}
"""
CALLS_FROM_EVALUATIONS = f"""
SELECT flag_key, {COLUMNS.format(response="response")}
FROM posthog.flag_evaluations
WHERE {WINDOW}
GROUP BY flag_key
LIMIT {FLAG_CALL_LIMIT}
"""


@dataclass(frozen=True)
class CreatedFlag:
    id: int
    key: str
    created_at: datetime
    removal_blocked: bool
    remote_configuration: bool


@dataclass(frozen=True)
class FlagCalls:
    people_this_week: int
    people_last_week: int
    calls_this_week: int
    calls: int
    values_this_week: int
    value_this_week: str | None


@dataclass(frozen=True)
class FlagCallSummary:
    by_key: Mapping[str, FlagCalls]
    complete: bool


def list_flags_created_by(team_id: int, user_id: int, *, limit: int) -> list[CreatedFlag]:
    flags = list(
        FeatureFlag.objects.filter(team_id=team_id, created_by_id=user_id, deleted=False)
        .only("id", "team_id", "key", "created_at", "is_remote_configuration")
        .order_by("created_at")[:limit]
    )
    if not flags:
        return []
    blocked = excluded_flag_ids(flags)
    return [
        CreatedFlag(
            id=flag.id,
            key=flag.key,
            created_at=flag.created_at,
            removal_blocked=flag.id in blocked,
            remote_configuration=flag.is_remote_configuration,
        )
        for flag in flags
    ]


def summarize_flag_calls(team: Team, user: User) -> FlagCallSummary:
    from_events = get_flag_evaluations_read_mode(team.organization_id) == FlagEvaluationsMode.EVENTS
    result = execute_hogql_query(
        CALLS_FROM_EVENTS if from_events else CALLS_FROM_EVALUATIONS,
        team,
        query_type="feature_flags_call_summary",
        user=user,
        settings=HogQLGlobalSettings(max_execution_time=QUERY_TIMEOUT_SECONDS),
    )
    rows = result.results or []
    return FlagCallSummary(
        by_key={row[0]: FlagCalls(*row[1:]) for row in rows if row[0]},
        complete=len(rows) < FLAG_CALL_LIMIT and all(row[0] for row in rows),
    )


__all__ = [
    "FLAG_CALL_DAYS",
    "CreatedFlag",
    "FlagCallSummary",
    "FlagCalls",
    "list_flags_created_by",
    "summarize_flag_calls",
]
