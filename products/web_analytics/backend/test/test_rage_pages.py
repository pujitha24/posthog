from datetime import datetime, timedelta

from posthog.test.base import BaseTest, ClickhouseTestMixin, _create_event, _create_person, flush_persons_and_events

from django.utils import timezone

from products.error_tracking.backend.facade.api import sync_issues_to_clickhouse
from products.error_tracking.backend.facade.testing import create_issue, create_issue_fingerprint
from products.web_analytics.backend.facade.rage_pages import (
    ErrorsBeforeRage,
    RagePage,
    find_errors_before_rage,
    find_top_rage_element,
    find_top_rage_page,
)


class TestRagePages(ClickhouseTestMixin, BaseTest):
    def _ago(self, hours: float) -> datetime:
        return timezone.now() - timedelta(hours=hours)

    def _rage_clicks(
        self, distinct_id: str, path: str, count: int, *, session_id: str = "", at: datetime | None = None
    ) -> None:
        start = at or self._ago(24)
        for i in range(count):
            _create_event(
                team=self.team,
                event="$rageclick",
                distinct_id=distinct_id,
                properties={"$pathname": path, "$session_id": session_id},
                elements_chain='button:text="Pay now"nth-child="0"nth-of-type="0"',
                timestamp=start + timedelta(seconds=i),
            )

    def test_the_top_page_needs_three_people_with_ten_rage_clicks_each_and_ignores_ids_in_the_path(self) -> None:
        for i, path in enumerate(["/checkout/123", "/checkout/456", "/checkout/789"]):
            _create_person(team=self.team, distinct_ids=[f"buyer-{i}"])
            self._rage_clicks(f"buyer-{i}", path, 10)
        _create_person(team=self.team, distinct_ids=["light"])
        self._rage_clicks("light", "/checkout/1", 9)
        for i in range(2):
            _create_person(team=self.team, distinct_ids=[f"home-{i}"])
            self._rage_clicks(f"home-{i}", "/home", 20)
        flush_persons_and_events()

        page = find_top_rage_page(self.team, self.user)

        self.assertEqual(
            (page, find_top_rage_element(self.team, self.user, "/checkout/…")),
            (RagePage(path="/checkout/…", people=3, clicks=30), "Pay now"),
        )

    def test_errors_before_rage_count_only_exceptions_in_the_30_seconds_before_the_first_click(self) -> None:
        issue_id = create_issue(team_id=self.team.id, name="TypeError")
        create_issue_fingerprint(team_id=self.team.id, issue_id=issue_id, fingerprint="fp-checkout")
        sync_issues_to_clickhouse(issue_ids=[issue_id], team_id=self.team.pk)
        for session, error_seconds_before in (("s1", 10), ("s2", 20), ("s3", 60)):
            _create_person(team=self.team, distinct_ids=[session])
            first_click = self._ago(24)
            _create_event(
                team=self.team,
                event="$exception",
                distinct_id=session,
                properties={"$session_id": session, "$exception_fingerprint": "fp-checkout"},
                timestamp=first_click - timedelta(seconds=error_seconds_before),
            )
            self._rage_clicks(session, "/checkout", 10, session_id=session, at=first_click)
        flush_persons_and_events()

        errors = find_errors_before_rage(self.team, self.user, "/checkout")

        self.assertEqual(errors, ErrorsBeforeRage(sessions=3, issue_id=str(issue_id), issue_sessions=2))
