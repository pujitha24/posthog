from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from django.utils import timezone

from posthog.hogql import ast
from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.query import execute_hogql_query

from products.error_tracking.backend.facade.contracts import (
    ErrorTrackingIssueBasics,
    NewIssueActivity,
    WeeklyExceptionTotals,
)
from products.error_tracking.backend.models import ErrorTrackingIssue

if TYPE_CHECKING:
    from posthog.models.team import Team
    from posthog.models.user import User

QUERY_TIMEOUT_SECONDS = 10
NEW_ISSUE_DAYS = 7
NEW_ISSUE_CANDIDATES = 50
CLIENT_LIBRARIES = (
    "web",
    "posthog-js",
    "posthog-js-lite",
    "posthog-ios",
    "posthog-android",
    "posthog-react-native",
    "posthog-flutter",
)
NEW_ISSUE_ACTIVITY = f"""
SELECT issue_id, min(issue_first_seen) AS first_seen, uniq(person_id) AS users, count() AS occurrences,
       countIf(properties.$lib IN {{client_libraries}}) / count() AS client_share
FROM events
WHERE event = '$exception' AND timestamp >= now() - INTERVAL {NEW_ISSUE_DAYS} DAY AND isNotNull(issue_id)
GROUP BY issue_id
HAVING first_seen >= now() - INTERVAL {NEW_ISSUE_DAYS} DAY
ORDER BY occurrences DESC
LIMIT {NEW_ISSUE_CANDIDATES}
"""
WEEKLY_TOTALS = """
SELECT countIf(event = '$exception') AS occurrences, uniqIf(person_id, event IN ('$pageview', '$screen')) AS active_users
FROM events
WHERE timestamp >= now() - INTERVAL 7 DAY AND event IN ('$pageview', '$screen', '$exception')
"""
ISSUES_WITH_EVENTS = """
SELECT DISTINCT toString(issue_id)
FROM events
WHERE event = '$exception' AND timestamp >= now() - INTERVAL {days} DAY AND toString(issue_id) IN {issue_ids}
LIMIT {limit}
"""


def _rows(query: str, team: "Team", user: "User", name: str, placeholders: dict[str, ast.Expr]) -> list[list]:
    return (
        execute_hogql_query(
            query,
            team,
            query_type=name,
            placeholders=placeholders,
            user=user,
            settings=HogQLGlobalSettings(max_execution_time=QUERY_TIMEOUT_SECONDS),
        ).results
        or []
    )


def new_unassigned_issue_activity(team: "Team", user: "User", *, limit: int) -> list[NewIssueActivity]:
    client_libraries = ast.Tuple(exprs=[ast.Constant(value=library) for library in CLIENT_LIBRARIES])
    rows = _rows(
        NEW_ISSUE_ACTIVITY, team, user, "error_tracking_new_issue_activity", {"client_libraries": client_libraries}
    )
    issues = (
        ErrorTrackingIssue.objects.with_first_seen()
        .filter(
            team_id=team.id,
            id__in=[UUID(str(row[0])) for row in rows],
            status=ErrorTrackingIssue.Status.ACTIVE,
            assignment__isnull=True,
        )
        .in_bulk()
    )
    since = timezone.now() - timedelta(days=NEW_ISSUE_DAYS)
    found = []
    for issue_id, _, users, occurrences, client_share in rows:
        issue = issues.get(UUID(str(issue_id)))
        if issue is None or issue.first_seen is None or issue.first_seen < since:
            continue
        found.append(
            NewIssueActivity(
                id=issue.id,
                name=issue.name,
                description=issue.description,
                first_seen=issue.first_seen,
                users=users,
                occurrences=occurrences,
                client_share=client_share,
            )
        )
    return found[:limit]


def weekly_totals(team: "Team", user: "User") -> WeeklyExceptionTotals:
    rows = _rows(WEEKLY_TOTALS, team, user, "error_tracking_weekly_totals", {})
    occurrences, active_users = rows[0] if rows else (0, 0)
    return WeeklyExceptionTotals(occurrences=occurrences, active_users=active_users)


def quiet_issues_assigned_to(
    team: "Team", user: "User", *, quiet_days: int, limit: int
) -> list[ErrorTrackingIssueBasics]:
    issues = list(
        ErrorTrackingIssue.objects.filter(
            team_id=team.id, status=ErrorTrackingIssue.Status.ACTIVE, assignment__user_id=user.id
        ).order_by("created_at")[:limit]
    )
    if not issues:
        return []
    issue_ids = ast.Tuple(exprs=[ast.Constant(value=str(issue.id)) for issue in issues])
    rows = _rows(
        ISSUES_WITH_EVENTS,
        team,
        user,
        "error_tracking_quiet_issues",
        {"days": ast.Constant(value=quiet_days), "issue_ids": issue_ids, "limit": ast.Constant(value=len(issues))},
    )
    loud = {row[0] for row in rows}
    return [
        ErrorTrackingIssueBasics(
            id=issue.id, name=issue.name, description=issue.description, status=issue.status, severity=issue.severity
        )
        for issue in issues
        if str(issue.id) not in loud
    ]
