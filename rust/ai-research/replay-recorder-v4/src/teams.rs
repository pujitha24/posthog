//! The teams that may be recorded: the replay teams of organizations that opt in to AI training.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use sqlx::PgPool;

/// The query the v3 mirror's team service runs, narrowed to the columns v4 reads.
const TEAMS: &str = "
    SELECT t.id::bigint, t.api_token, COALESCE(o.is_ai_training_opted_in, false)
    FROM posthog_team t
    LEFT JOIN posthog_organization o ON o.id = t.organization_id
    WHERE t.session_recording_opt_in = true
";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Team {
    pub id: i64,
    pub ai_training_opted_in: bool,
}

/// Teams by project token. Lookups read a snapshot, so the consume loop never waits on Postgres.
#[derive(Default)]
pub struct TeamCache {
    teams: ArcSwap<HashMap<String, Team>>,
}

impl TeamCache {
    pub fn from_teams(teams: HashMap<String, Team>) -> Self {
        Self {
            teams: ArcSwap::from_pointee(teams),
        }
    }

    pub fn get(&self, token: &str) -> Option<Team> {
        self.teams.load().get(token).copied()
    }

    pub async fn refresh(&self, pool: &PgPool) -> Result<usize, sqlx::Error> {
        let rows: Vec<(i64, String, bool)> = sqlx::query_as(TEAMS).fetch_all(pool).await?;
        let teams: HashMap<String, Team> = rows
            .into_iter()
            .map(|(id, token, opted_in)| {
                (
                    token,
                    Team {
                        id,
                        ai_training_opted_in: opted_in,
                    },
                )
            })
            .collect();
        let count = teams.len();
        self.teams.store(Arc::new(teams));
        metrics::gauge!("replay_recorder_v4_teams").set(count as f64);
        Ok(count)
    }

    /// Refreshes forever. A failed refresh keeps the previous list, which is at most one interval stale.
    pub async fn refresh_every(self: Arc<Self>, pool: PgPool, interval: Duration) {
        let mut ticker = tokio::time::interval(interval);
        ticker.tick().await;
        loop {
            ticker.tick().await;
            if let Err(error) = self.refresh(&pool).await {
                tracing::warn!(%error, "team refresh failed");
                metrics::counter!("replay_recorder_v4_team_refresh_failures_total").increment(1);
            }
        }
    }
}
