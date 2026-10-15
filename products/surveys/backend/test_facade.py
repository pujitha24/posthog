import uuid
from datetime import timedelta

from posthog.test.base import BaseTest, ClickhouseTestMixin, _create_event, flush_persons_and_events

from django.utils import timezone

from products.surveys.backend.facade.api import SurveyResponses, list_running_survey_responses
from products.surveys.backend.models import Survey


class TestListRunningSurveyResponses(ClickhouseTestMixin, BaseTest):
    def _survey(self, name: str, **fields: object) -> Survey:
        return Survey.objects.create(
            **{
                "team": self.team,
                "name": name,
                "type": "popover",
                "created_by": self.user,
                "start_date": timezone.now() - timedelta(days=30),
                **fields,
            }
        )

    def _sent(self, survey: Survey, days_ago: int, submission_id: str | None) -> None:
        properties: dict[str, object] = {"$survey_id": str(survey.id)}
        if submission_id:
            properties["$survey_submission_id"] = submission_id
        _create_event(
            team=self.team,
            event="survey sent",
            distinct_id=str(uuid.uuid4()),
            properties=properties,
            timestamp=timezone.now() - timedelta(days=days_ago),
        )

    def test_counts_unique_submissions_of_your_running_surveys_this_week_and_last_week(self) -> None:
        running = self._survey("Onboarding feedback")
        self._survey("Ended", end_date=timezone.now() - timedelta(days=1))
        self._survey("Draft", start_date=None)
        self._survey("Archived", archived=True)
        self._survey("Not yours", created_by=self._create_user("other@example.com"))
        self._sent(running, 1, "submission-a")
        self._sent(running, 2, "submission-a")
        self._sent(running, 2, "submission-b")
        self._sent(running, 3, None)
        self._sent(running, 10, "submission-c")
        self._sent(running, 20, "submission-d")
        flush_persons_and_events()

        assert list_running_survey_responses(self.team, self.user, limit=20) == [
            SurveyResponses(survey_id=running.id, name="Onboarding feedback", this_week=3, last_week=1)
        ]
