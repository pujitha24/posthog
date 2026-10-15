from datetime import UTC, date, datetime, time, timedelta

from posthog.test.base import BaseTest
from unittest.mock import patch

from parameterized import parameterized

from products.experiments.backend.facade.contracts import EndedExperiment, RunningExperiment
from products.today.backend.logic.highlight_registry import load_sources
from products.today.backend.logic.highlight_sources import experiments
from products.today.backend.logic.highlight_types import Context

MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 13)


def noon(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=UTC)


class TestExperiments(BaseTest):
    def _find(self, today: date, ended: list[EndedExperiment], running: list[RunningExperiment], enrolled: int):
        registered = next(s for s in load_sources() if s.name == "experiments")
        with (
            patch.object(experiments, "list_ended_experiments_without_conclusion", return_value=ended),
            patch.object(experiments, "list_running_experiments", return_value=running),
            patch.object(experiments, "count_enrolled_people", return_value={"onboarding-tips": enrolled}),
        ):
            return registered.find(Context(self.team, self.user, today))

    def test_an_ended_experiment_without_a_decision_is_an_opportunity(self) -> None:
        ended = EndedExperiment(id=7, name="Pricing page layout experiment", ended_at=noon(TUESDAY - timedelta(days=1)))

        found = self._find(TUESDAY, [ended], [], 0)

        self.assertEqual(
            [(c.title, c.detail, c.url) for c in found.items],
            [
                (
                    "Experiment ‘Pricing page layout’ ended yesterday",
                    "Read the results and decide whether to release the change",
                    "/experiments/7",
                )
            ],
        )

    @parameterized.expand(
        [
            (
                "few users on a Monday",
                MONDAY,
                80,
                42,
                "Experiment ‘Onboarding tips’ reached 42 users in the last 14 days",
            ),
            (
                "no users on a Monday",
                MONDAY,
                80,
                0,
                "Experiment ‘Onboarding tips’ reached no users in the last 14 days",
            ),
            ("enough users", MONDAY, 80, 150, None),
            ("an old experiment on a Tuesday", TUESDAY, 80, 42, None),
            (
                "the week it crosses 60 days",
                TUESDAY,
                62,
                1,
                "Experiment ‘Onboarding tips’ reached 1 user in the last 14 days",
            ),
        ]
    )
    def test_a_long_running_experiment_with_few_users_is_a_risk(
        self, _name: str, today: date, days_running: int, enrolled: int, title: str | None
    ) -> None:
        running = RunningExperiment(
            id=9,
            name="Onboarding tips",
            started_at=noon(today - timedelta(days=days_running)),
            flag_key="onboarding-tips",
        )

        found = self._find(today, [], [running], enrolled)

        self.assertEqual([c.title for c in found.items], [title] if title else [])
