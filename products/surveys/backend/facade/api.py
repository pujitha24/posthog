from dataclasses import dataclass
from uuid import UUID

from posthog.hogql import ast
from posthog.hogql.query import execute_hogql_query

from posthog.models import Team, User

from products.surveys.backend.desktop_feedback import (
    DesktopFeedbackUnavailable,
    read_desktop_feedback_media,
    submit_desktop_feedback,
)
from products.surveys.backend.models import Survey
from products.surveys.backend.responses.fetch_rows import SUBMISSION_GROUPING_KEY

WEEKLY_RESPONSES = f"""
SELECT properties.$survey_id AS survey_id,
       uniqIf({SUBMISSION_GROUPING_KEY}, timestamp >= now() - INTERVAL 7 DAY) AS this_week,
       uniqIf({SUBMISSION_GROUPING_KEY}, timestamp < now() - INTERVAL 7 DAY) AS last_week
FROM events
WHERE timestamp >= now() - INTERVAL 14 DAY AND event = 'survey sent' AND properties.$survey_id IN {{survey_ids}}
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
        .order_by("-created_at")
        .values_list("id", "name")[:limit]
    )
    if not surveys:
        return []
    result = execute_hogql_query(
        WEEKLY_RESPONSES,
        team,
        query_type="surveys_running_weekly_responses",
        placeholders={"survey_ids": ast.Tuple(exprs=[ast.Constant(value=str(survey_id)) for survey_id, _ in surveys])},
        user=user,
    )
    counts = {str(survey_id): (this_week, last_week) for survey_id, this_week, last_week in result.results or []}
    return [
        SurveyResponses(
            survey_id=survey_id,
            name=name,
            this_week=counts.get(str(survey_id), (0, 0))[0],
            last_week=counts.get(str(survey_id), (0, 0))[1],
        )
        for survey_id, name in surveys
    ]


__all__ = [
    "DesktopFeedbackUnavailable",
    "SurveyResponses",
    "list_running_survey_responses",
    "read_desktop_feedback_media",
    "submit_desktop_feedback",
]
