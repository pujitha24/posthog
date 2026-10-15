import json
import hashlib
from datetime import timedelta
from typing import Any, cast

from django.utils import timezone

from posthog.schema import HogQLQueryModifiers

from posthog.hogql import ast
from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.parser import parse_expr, parse_select
from posthog.hogql.property import property_to_expr
from posthog.hogql.query import execute_hogql_query
from posthog.hogql.visitor import clone_expr

from posthog.models import Team, User

from products.actions.backend.facade.api import action_filter_conditions
from products.customer_analytics.backend.facade.contracts import KeyMetric

KEY_METRICS = (
    ("signup_event", "Sign-ups", False),
    ("payment_event", "Payments", False),
    ("subscription_event", "Subscriptions", False),
    ("activity_event", "Active users", True),
)
DAYS = 35
QUERY_TIMEOUT_SECONDS = 10
DAILY = "SELECT toDate(timestamp) AS day FROM events WHERE timestamp >= toStartOfDay(now()) - INTERVAL {days} DAY AND timestamp < toStartOfDay(now()) AND {match} GROUP BY day ORDER BY day"
WEEKLY = "SELECT 1 FROM events WHERE timestamp >= toStartOfDay(now()) - INTERVAL 14 DAY AND timestamp < toStartOfDay(now()) AND {match}"
THIS_WEEK = "timestamp >= toStartOfDay(now()) - INTERVAL 7 DAY"


def key_metrics_fingerprint(team: Team) -> str:
    config = team.customer_analytics_config
    nodes = {key: getattr(config, key, None) for key, _name, _people in KEY_METRICS}
    action_ids = [
        int(node["id"]) for node in nodes.values() if isinstance(node, dict) and node.get("kind") == "ActionsNode"
    ]
    actions = {
        action_id: repr(condition)
        for action_id, condition in action_filter_conditions(team=team, action_ids=action_ids).items()
    }
    return hashlib.sha256(json.dumps([nodes, actions], sort_keys=True, default=str).encode()).hexdigest()[:16]


def key_metrics(team: Team, user: User) -> list[KeyMetric]:
    config = team.customer_analytics_config
    nodes: list[tuple[str, str, bool, dict[str, Any]]] = [
        (key, node.get("custom_name") or name, people, node)
        for key, name, people in KEY_METRICS
        if isinstance(node := getattr(config, key, None), dict) and node.get("kind") in ("EventsNode", "ActionsNode")
    ]
    actions = action_filter_conditions(
        team=team, action_ids=[int(node["id"]) for *_, node in nodes if node["kind"] == "ActionsNode"]
    )
    nodes = [entry for entry in nodes if entry[3]["kind"] != "ActionsNode" or int(entry[3]["id"]) in actions]
    if not nodes:
        return []

    def condition(node: dict[str, Any]) -> ast.Expr:
        if node["kind"] == "ActionsNode":
            match: ast.Expr = clone_expr(actions[int(node["id"])])
        elif node.get("event"):
            match = ast.CompareOperation(
                op=ast.CompareOperationOp.Eq, left=ast.Field(chain=["event"]), right=ast.Constant(value=node["event"])
            )
        else:
            match = ast.Constant(value=True)
        if node.get("properties"):
            match = ast.And(exprs=[match, property_to_expr(node["properties"], team)])
        return match

    daily = _select(DAILY, {"days": ast.Constant(value=DAYS), "match": ast.Or(exprs=[condition(n) for *_, n in nodes])})
    daily.select.extend(
        ast.Alias(
            alias=f"m{index}",
            expr=ast.Call(name="uniqIf", args=[ast.Field(chain=["person_id"]), condition(node)])
            if people
            else ast.Call(name="countIf", args=[condition(node)]),
        )
        for index, (_key, _name, people, node) in enumerate(nodes)
    )
    by_day = {row[0]: row[1:] for row in _rows(daily, team, user, "customer_analytics_key_metrics_daily")}
    weekly_people = iter(_weekly_people(team, user, [condition(node) for _key, _name, people, node in nodes if people]))

    today = timezone.now().astimezone(team.timezone_info).date()
    days = [today - timedelta(days=offset) for offset in range(DAYS, 0, -1)]
    metrics = []
    for index, (key, name, people, _node) in enumerate(nodes):
        points = [(day, float(by_day[day][index]) if day in by_day else 0.0) for day in days]
        if people:
            this_week, last_week = next(weekly_people), next(weekly_people)
        else:
            this_week, last_week = sum(v for _, v in points[-7:]), sum(v for _, v in points[-14:-7])
        metrics.append(
            KeyMetric(key=key, name=name, people=people, daily=points, this_week=this_week, last_week=last_week)
        )
    return metrics


def _weekly_people(team: Team, user: User, conditions: list[ast.Expr]) -> list[float]:
    if not conditions:
        return []
    weekly = _select(WEEKLY, {"match": ast.Or(exprs=[clone_expr(c) for c in conditions])})
    weekly.select = [
        ast.Call(
            name="uniqIf",
            args=[ast.Field(chain=["person_id"]), ast.And(exprs=[clone_expr(c), period])],
        )
        for c in conditions
        for period in (parse_expr(THIS_WEEK), ast.Not(expr=parse_expr(THIS_WEEK)))
    ]
    rows = _rows(weekly, team, user, "customer_analytics_key_metrics_weekly")
    return [float(value) for value in rows[0]] if rows else [0.0] * (2 * len(conditions))


def _select(query: str, placeholders: dict[str, ast.Expr]) -> ast.SelectQuery:
    return cast(ast.SelectQuery, parse_select(query, placeholders=placeholders))


def _rows(query: ast.SelectQuery, team: Team, user: User, name: str) -> list[list]:
    return (
        execute_hogql_query(
            query,
            team,
            query_type=name,
            user=user,
            modifiers=HogQLQueryModifiers(convertToProjectTimezone=True),
            settings=HogQLGlobalSettings(max_execution_time=QUERY_TIMEOUT_SECONDS, timeout_overflow_mode="throw"),
        ).results
        or []
    )
