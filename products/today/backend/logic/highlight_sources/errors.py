from django.core.cache import cache
from django.utils import timezone

from products.error_tracking.backend.facade.api import (
    get_weekly_exception_totals,
    list_new_unassigned_issue_activity,
    list_quiet_issues_assigned_to,
)
from products.error_tracking.backend.facade.contracts import NewIssueActivity, WeeklyExceptionTotals

from ...facade.enums import HighlightLabel
from ..highlight_rules import human, short, users, uses_product
from ..highlight_types import Candidate, Context, Found, Source

NEW_CLIENT_ERROR = "{users} saw the new error ‘{issue}’"
NEW_CLIENT_ERROR_DETAIL = "{started} · {share} of active users · No owner"
NEW_SERVER_ERROR = "New server error ‘{issue}’ is {share} of all errors"
NEW_SERVER_ERROR_DETAIL = "{started} · {events} events in the last 7 days · No owner"
ASSIGNED_QUIET_ONE = "Your issue ‘{issue}’ had no events in the last 14 days"
ASSIGNED_QUIET_ONE_DETAIL = "Resolve it if the fix is live"
ASSIGNED_QUIET_MANY = "{n} of your issues had no events in the last 14 days"
AND_MORE = "{names} and {n} more"

NEW_ISSUES = 5
QUIET_DAYS = 14
QUIET_ISSUES = 500
MIN_CLIENT_USERS = 50
MIN_CLIENT_SHARE = 0.002
MIN_SERVER_SHARE = 0.02
DAY_SECONDS = 24 * 60 * 60


def find(context: Context) -> Found:
    return Found(items=(*_new_errors(context), *_quiet_assigned(context)))


def _issue_name(name: str | None, description: str | None) -> str:
    return f"{name or 'Issue'}: {description}" if description else name or "Issue"


def _new_errors(context: Context) -> list[Candidate]:
    issues = list_new_unassigned_issue_activity(context.team, context.user, limit=NEW_ISSUES)
    if not issues:
        return []
    totals = _daily_totals(context)
    relevant = uses_product(context, "Error tracking")
    candidates = [_new_error(context, issue, totals, relevant) for issue in issues]
    return [candidate for candidate in candidates if candidate is not None]


def _daily_totals(context: Context) -> WeeklyExceptionTotals:
    key = f"today_highlights:exception_totals:v1:{context.team.id}:{context.restrictions}:{context.today.isoformat()}"
    cached = cache.get(key)
    if isinstance(cached, WeeklyExceptionTotals):
        return cached
    totals = get_weekly_exception_totals(context.team, context.user)
    cache.set(key, totals, DAY_SECONDS)
    return totals


def _new_error(
    context: Context, issue: NewIssueActivity, totals: WeeklyExceptionTotals, relevant: bool
) -> Candidate | None:
    name = short(_issue_name(issue.name, issue.description), 60)
    started = f"Since {timezone.localtime(issue.first_seen, context.team.timezone_info).strftime('%b %-d')}"
    if issue.client_share >= 0.8 and MIN_CLIENT_USERS <= issue.users <= totals.active_users:
        share = issue.users / totals.active_users
        if share < MIN_CLIENT_SHARE:
            return None
        importance = min(1.0, 0.5 + share / 0.02)
        title = NEW_CLIENT_ERROR.format(issue=name, users=users(issue.users))
        detail = NEW_CLIENT_ERROR_DETAIL.format(started=started, share=f"{share * 100:.1f}%")
    elif issue.client_share < 0.2 and totals.occurrences:
        share = issue.occurrences / totals.occurrences
        if share < MIN_SERVER_SHARE:
            return None
        importance = min(0.7, 0.4 + share / 0.1)
        title = NEW_SERVER_ERROR.format(issue=name, share=f"{share * 100:.0f}%")
        detail = NEW_SERVER_ERROR_DETAIL.format(started=started, events=human(issue.occurrences))
    else:
        return None
    if not relevant and importance < 0.9:
        return None
    return Candidate(
        key=f"new_error:{issue.id}",
        label=HighlightLabel.RISK,
        family="errors",
        importance=importance,
        relevance=1.0 if relevant else 0.0,
        title=title,
        detail=detail,
        url=f"/error_tracking/{issue.id}",
        ref=str(issue.id),
    )


def _quiet_assigned(context: Context) -> list[Candidate]:
    issues = list_quiet_issues_assigned_to(context.team, context.user, quiet_days=QUIET_DAYS, limit=QUIET_ISSUES)
    if not issues:
        return []
    names = [short(_issue_name(issue.name, issue.description), 50) for issue in issues]
    one = len(issues) == 1
    return [
        Candidate(
            key="assigned_quiet",
            label=HighlightLabel.OPPORTUNITY,
            family="errors",
            importance=0.35,
            relevance=1.0,
            title=ASSIGNED_QUIET_ONE.format(issue=names[0]) if one else ASSIGNED_QUIET_MANY.format(n=len(issues)),
            detail=ASSIGNED_QUIET_ONE_DETAIL if one else AND_MORE.format(names=f"‘{names[0]}’", n=len(names) - 1),
            url=f"/error_tracking/{issues[0].id}" if one else "/error_tracking",
        )
    ]


SOURCE = Source(name="errors", find=find, scopes=("error_tracking:read",))
