from dataclasses import dataclass
from uuid import UUID

from posthog.hogql import ast
from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.parser import parse_expr
from posthog.hogql.query import execute_hogql_query

from posthog.models import Team, User

from products.surveys.backend.desktop_feedback import (
    DesktopFeedbackUnavailable,
    read_desktop_feedback_media,
    submit_desktop_feedback,
)
from products.surveys.backend.models import Survey
from products.surveys.backend.responses.fetch_rows import RESPONSE_EVENT_FILTER, SUBMISSION_GROUPING_KEY

QUERY_TIMEOUT_SECONDS = 10
WEEKLY_RESPONSES = """
SELECT properties.$survey_id AS survey_id,
       uniqIf({grouping_key}, timestamp >= now() - INTERVAL 7 DAY) AS this_week,
       uniqIf({grouping_key}, timestamp < now() - INTERVAL 7 DAY) AS last_week
FROM events
WHERE timestamp >= now() - INTERVAL 14 DAY AND {response_events} AND properties.$survey_id IN {survey_ids}
GROUP BY survey_id
"""


@dataclass(frozen=True)
class SurveyResponses:
    survey_id: UUID
    name: str
    this_week: int
    last_week: int


def list_running_survey_responses(team: Team, user: User, *, limit: int) -> list[SurveyResponses]:
    surveys = list(
        Survey.objects.filter(
            team_id=team.id, created_by_id=user.id, start_date__isnull=False, end_date__isnull=True, archived=False
        )
        .exclude(product_tour__isnull=False)
        .order_by("-created_at")
        .values_list("id", "name")[:limit]
    )
    if not surveys:
        return []
    result = execute_hogql_query(
        WEEKLY_RESPONSES,
        team,
        query_type="surveys_running_weekly_responses",
        placeholders={
            "grouping_key": parse_expr(SUBMISSION_GROUPING_KEY),
            "response_events": parse_expr(RESPONSE_EVENT_FILTER),
            "survey_ids": ast.Tuple(exprs=[ast.Constant(value=str(survey_id)) for survey_id, _ in surveys]),
        },
        user=user,
        settings=HogQLGlobalSettings(max_execution_time=QUERY_TIMEOUT_SECONDS, timeout_overflow_mode="throw"),
    )
    counts = {survey_id: (this_week, last_week) for survey_id, this_week, last_week in result.results}
    responses = []
    for survey_id, name in surveys:
        this_week, last_week = counts.get(str(survey_id), (0, 0))
        responses.append(SurveyResponses(survey_id=survey_id, name=name, this_week=this_week, last_week=last_week))
    return responses


__all__ = [
    "DesktopFeedbackUnavailable",
    "SurveyResponses",
    "list_running_survey_responses",
    "read_desktop_feedback_media",
    "submit_desktop_feedback",
]
