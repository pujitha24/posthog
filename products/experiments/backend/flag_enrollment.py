from posthog.hogql import ast
from posthog.hogql.query import execute_hogql_query

from posthog.models.team import Team
from posthog.models.user import User

from products.feature_flags.backend.facade.enums import FlagEvaluationsMode
from products.feature_flags.backend.facade.flags import get_flag_evaluations_read_mode

WINDOW = "timestamp >= toStartOfDay(now()) - INTERVAL 14 DAY AND timestamp < toStartOfDay(now())"
ENROLLED_FROM_EVENTS = f"""
SELECT properties.$feature_flag AS flag_key, uniq(person_id) AS people
FROM events
WHERE event = '$feature_flag_called' AND {WINDOW}
  AND properties.$feature_flag IN {{flag_keys}}
  AND toString(properties.$feature_flag_response) NOT IN ('', 'false')
GROUP BY flag_key
"""
ENROLLED_FROM_EVALUATIONS = f"""
SELECT flag_key, uniq(person_id) AS people
FROM posthog.flag_evaluations
WHERE {WINDOW} AND flag_key IN {{flag_keys}} AND toString(response) NOT IN ('', 'false')
GROUP BY flag_key
"""


def enrolled_people_by_flag(team: Team, user: User, flag_keys: list[str]) -> dict[str, int]:
    if not flag_keys:
        return {}
    from_events = get_flag_evaluations_read_mode(team.organization_id) == FlagEvaluationsMode.EVENTS
    result = execute_hogql_query(
        ENROLLED_FROM_EVENTS if from_events else ENROLLED_FROM_EVALUATIONS,
        team,
        query_type="experiments_enrolled_people_by_flag",
        placeholders={"flag_keys": ast.Tuple(exprs=[ast.Constant(value=key) for key in flag_keys])},
        user=user,
    )
    return dict(result.results or [])
