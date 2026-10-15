//! Resolves the keys for one recorder flush, storing the new ones before anything is sealed with them.

use std::collections::{BTreeSet, HashMap};

use futures::stream::{self, StreamExt, TryStreamExt};
use rand::RngCore;

use super::kms::{CachedKms, KeyWrapper, KmsError};
use super::rows::{Row, TableKey};
use super::table::{KeyTable, PutOutcome, TableError};
use crate::envelope::EnvelopeError;
use crate::seal::{open_session_key, seal_session_key, SealedKey, WrappingContext};

/// How many conditional puts one flush runs at once, as in v3.
const PUT_CONCURRENCY: usize = 32;

#[derive(Debug, thiserror::Error)]
pub enum KeyError {
    #[error(transparent)]
    Table(#[from] TableError),
    #[error(transparent)]
    Kms(#[from] KmsError),
    #[error("a stored session key does not open: {0}")]
    Seal(#[from] EnvelopeError),
}

/// A session in a flush.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SessionId {
    pub team_id: i64,
    pub session_id: String,
}

/// Why a flush leaves a session out.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DropCause {
    /// A deletion tombstoned the session or its team.
    Deleted,
    /// The session's key row names a team day key that is gone, so nothing sealed for it can be opened.
    KeyLost,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionKey {
    pub key: [u8; 32],
    /// The day the session key was first stored. Its row expires with that day.
    pub key_day: String,
}

/// The keys a flush seals with.
#[derive(Debug, Default)]
pub struct FlushKeys {
    pub sessions: HashMap<SessionId, SessionKey>,
    /// Team day keys of the write day, for the flush's images.
    pub team_days: HashMap<i64, [u8; 32]>,
    pub dropped: Vec<(SessionId, DropCause)>,
}

enum TeamDay {
    Key([u8; 32]),
    Deleted,
}

pub struct KeyStore<T, W> {
    table: T,
    kms: CachedKms<W>,
}

impl<T: KeyTable, W: KeyWrapper> KeyStore<T, W> {
    pub fn new(table: T, kms: CachedKms<W>) -> Self {
        Self { table, kms }
    }

    /// Resolves a key for every session, storing the new ones. Call it before sealing anything for the flush: a key
    /// that is not stored yet cannot open what it sealed if this process dies.
    pub async fn keys_for_flush(
        &self,
        sessions: &[SessionId],
        write_day: &str,
    ) -> Result<FlushKeys, KeyError> {
        let teams: BTreeSet<i64> = sessions.iter().map(|s| s.team_id).collect();
        let mut keys: Vec<TableKey> = teams
            .iter()
            .map(|&team| TableKey::team_day(team, write_day))
            .collect();
        keys.extend(
            sessions
                .iter()
                .map(|s| TableKey::session(s.team_id, &s.session_id)),
        );
        let mut rows = self.table.batch_get(&keys).await?;

        // A session stored on an earlier day is sealed under that day's team key, which the first read did not ask for.
        let earlier: BTreeSet<TableKey> = rows
            .values()
            .filter_map(|row| match row {
                Row::Session {
                    team_id, key_day, ..
                } if key_day != write_day => Some(TableKey::team_day(*team_id, key_day)),
                _ => None,
            })
            .collect();
        if !earlier.is_empty() {
            rows.extend(
                self.table
                    .batch_get(&earlier.into_iter().collect::<Vec<_>>())
                    .await?,
            );
        }

        let mut team_days: HashMap<(i64, String), TeamDay> = HashMap::new();
        for &team in &teams {
            let team_day = self
                .write_day_key(
                    team,
                    write_day,
                    rows.get(&TableKey::team_day(team, write_day)),
                )
                .await?;
            team_days.insert((team, write_day.to_owned()), team_day);
        }

        let mut flush = FlushKeys::default();
        let mut new_sessions = Vec::new();
        for session in sessions {
            match rows.get(&TableKey::session(session.team_id, &session.session_id)) {
                Some(Row::Session {
                    key_day, sealed, ..
                }) => {
                    match self
                        .stored_session_key(session, key_day, sealed, &rows, &mut team_days)
                        .await?
                    {
                        Ok(key) => {
                            flush.sessions.insert(session.clone(), key);
                        }
                        Err(cause) => flush.dropped.push((session.clone(), cause)),
                    }
                }
                Some(_) => flush.dropped.push((session.clone(), DropCause::Deleted)),
                None => match &team_days[&(session.team_id, write_day.to_owned())] {
                    TeamDay::Key(team_key) => new_sessions.push((session.clone(), *team_key)),
                    TeamDay::Deleted => flush.dropped.push((session.clone(), DropCause::Deleted)),
                },
            }
        }

        let stored: Vec<_> = stream::iter(new_sessions)
            .map(|(session, team_key)| self.store_new_session(session, team_key, write_day))
            .buffer_unordered(PUT_CONCURRENCY)
            .try_collect()
            .await?;
        for (session, outcome) in stored {
            match outcome {
                NewSession::Key(key) => {
                    flush.sessions.insert(session, key);
                }
                NewSession::Winner(key_day, sealed) => {
                    let row_lookup = HashMap::new();
                    match self
                        .stored_session_key(
                            &session,
                            &key_day,
                            &sealed,
                            &row_lookup,
                            &mut team_days,
                        )
                        .await?
                    {
                        Ok(key) => {
                            flush.sessions.insert(session, key);
                        }
                        Err(cause) => flush.dropped.push((session, cause)),
                    }
                }
                NewSession::Deleted => flush.dropped.push((session, DropCause::Deleted)),
            }
        }

        for ((team, day), team_day) in team_days {
            if let (TeamDay::Key(key), true) = (team_day, day == write_day) {
                flush.team_days.insert(team, key);
            }
        }
        Ok(flush)
    }

    /// The team key of the write day: the stored one, or a new one stored now.
    async fn write_day_key(
        &self,
        team: i64,
        write_day: &str,
        row: Option<&Row>,
    ) -> Result<TeamDay, KeyError> {
        let context = WrappingContext {
            team_id: team,
            key_day: write_day.to_owned(),
            session_id: None,
        };
        let wrapped = match row {
            Some(Row::TeamDay { wrapped_key, .. }) => wrapped_key.clone(),
            Some(_) => return Ok(TeamDay::Deleted),
            None => {
                let generated = self.kms.generate(&context).await?;
                let candidate = Row::TeamDay {
                    team_id: team,
                    key_day: write_day.to_owned(),
                    wrapped_key: generated.wrapped.clone(),
                };
                match self
                    .table
                    .put_if_absent(&TableKey::team_day(team, write_day), &candidate)
                    .await?
                {
                    PutOutcome::Stored => {
                        self.kms.remember(&context, &generated).await;
                        return Ok(TeamDay::Key(generated.plaintext));
                    }
                    PutOutcome::Existing(Row::TeamDay { wrapped_key, .. }) => wrapped_key,
                    PutOutcome::Existing(_) => return Ok(TeamDay::Deleted),
                }
            }
        };
        Ok(TeamDay::Key(self.kms.unwrap(&context, &wrapped).await?))
    }

    async fn stored_session_key(
        &self,
        session: &SessionId,
        key_day: &str,
        sealed: &SealedKey,
        rows: &HashMap<TableKey, Row>,
        team_days: &mut HashMap<(i64, String), TeamDay>,
    ) -> Result<Result<SessionKey, DropCause>, KeyError> {
        let slot = (session.team_id, key_day.to_owned());
        if !team_days.contains_key(&slot) {
            let key = TableKey::team_day(session.team_id, key_day);
            let row = match rows.get(&key) {
                Some(row) => Some(row.clone()),
                None => self
                    .table
                    .batch_get(std::slice::from_ref(&key))
                    .await?
                    .remove(&key),
            };
            let team_day = match row {
                Some(Row::TeamDay { wrapped_key, .. }) => {
                    let context = WrappingContext {
                        team_id: session.team_id,
                        key_day: key_day.to_owned(),
                        session_id: None,
                    };
                    TeamDay::Key(self.kms.unwrap(&context, &wrapped_key).await?)
                }
                Some(Row::Deleted) => TeamDay::Deleted,
                _ => return Ok(Err(DropCause::KeyLost)),
            };
            team_days.insert(slot.clone(), team_day);
        }
        let TeamDay::Key(team_key) = &team_days[&slot] else {
            return Ok(Err(DropCause::Deleted));
        };
        let context = WrappingContext {
            team_id: session.team_id,
            key_day: key_day.to_owned(),
            session_id: Some(session.session_id.clone()),
        };
        let key = open_session_key(team_key, &context, sealed)?;
        Ok(Ok(SessionKey {
            key,
            key_day: key_day.to_owned(),
        }))
    }

    async fn store_new_session(
        &self,
        session: SessionId,
        team_key: [u8; 32],
        write_day: &str,
    ) -> Result<(SessionId, NewSession), KeyError> {
        let mut key = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut key);
        let context = WrappingContext {
            team_id: session.team_id,
            key_day: write_day.to_owned(),
            session_id: Some(session.session_id.clone()),
        };
        let row = Row::Session {
            team_id: session.team_id,
            key_day: write_day.to_owned(),
            sealed: seal_session_key(&team_key, &context, &key),
        };
        let outcome = match self
            .table
            .put_if_absent(
                &TableKey::session(session.team_id, &session.session_id),
                &row,
            )
            .await?
        {
            PutOutcome::Stored => NewSession::Key(SessionKey {
                key,
                key_day: write_day.to_owned(),
            }),
            PutOutcome::Existing(Row::Session {
                key_day, sealed, ..
            }) => NewSession::Winner(key_day, sealed),
            PutOutcome::Existing(_) => NewSession::Deleted,
        };
        Ok((session, outcome))
    }
}

enum NewSession {
    Key(SessionKey),
    /// Another writer stored this session's key first, possibly on another day. The flush adopts it.
    Winner(String, SealedKey),
    Deleted,
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use async_trait::async_trait;

    use super::*;
    use crate::keys::kms::DataKey;
    use crate::keys::memory::MemoryTable;

    /// Wraps a key by prefixing its context, so unwrapping under another context fails as KMS would.
    #[derive(Default)]
    struct FakeKms;

    fn label(context: &WrappingContext) -> Vec<u8> {
        format!("{:?}", context.entries()).into_bytes()
    }

    #[async_trait]
    impl KeyWrapper for FakeKms {
        async fn generate(&self, context: &WrappingContext) -> Result<DataKey, KmsError> {
            let plaintext: [u8; 32] = rand::random();
            Ok(DataKey {
                plaintext,
                wrapped: [label(context), plaintext.to_vec()].concat(),
            })
        }

        async fn unwrap(
            &self,
            context: &WrappingContext,
            wrapped: &[u8],
        ) -> Result<[u8; 32], KmsError> {
            let label = label(context);
            if !wrapped.starts_with(&label) {
                return Err(KmsError::Request("encryption context mismatch".to_owned()));
            }
            wrapped[label.len()..]
                .try_into()
                .map_err(|_| KmsError::InvalidKey)
        }
    }

    fn store(table: MemoryTable) -> KeyStore<MemoryTable, FakeKms> {
        KeyStore::new(table, CachedKms::new(FakeKms, 100, Duration::from_secs(60)))
    }

    fn session(team_id: i64, n: u32) -> SessionId {
        SessionId {
            team_id,
            session_id: format!("019a2f3e-8c00-7000-8000-{n:012}"),
        }
    }

    /// Another pod: a fresh process that reads what the first one stored.
    fn rows_of(store: &KeyStore<MemoryTable, FakeKms>) -> MemoryTable {
        MemoryTable {
            rows: std::sync::Mutex::new(store.table.rows.lock().unwrap().clone()),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn a_new_session_key_is_stored_and_found_again_on_a_later_day() {
        let first = store(MemoryTable::default());
        let a = session(7, 1);
        let day_one = first
            .keys_for_flush(std::slice::from_ref(&a), "2026-10-15")
            .await
            .unwrap();
        let key = day_one.sessions[&a].clone();
        assert_eq!(key.key_day, "2026-10-15");

        // The session keeps its first key across a new day, while the team gets that day's key for its images.
        let second = store(rows_of(&first));
        let day_two = second
            .keys_for_flush(std::slice::from_ref(&a), "2026-10-16")
            .await
            .unwrap();
        assert_eq!(day_two.sessions[&a], key);
        assert_ne!(day_two.team_days[&7], day_one.team_days[&7]);
    }

    #[tokio::test]
    async fn adopts_the_key_another_writer_stored_first() {
        let a = session(7, 1);
        let winner = store(MemoryTable::default());
        let winning = winner
            .keys_for_flush(std::slice::from_ref(&a), "2026-10-15")
            .await
            .unwrap();

        // The loser read before the winner wrote, so it reaches the conditional put with a candidate of its own.
        let loser = store(rows_of(&winner));
        let (_, outcome) = loser
            .store_new_session(a.clone(), [9; 32], "2026-10-15")
            .await
            .unwrap();
        let NewSession::Winner(key_day, sealed) = outcome else {
            panic!("the put must lose")
        };
        let adopted = loser
            .stored_session_key(&a, &key_day, &sealed, &HashMap::new(), &mut HashMap::new())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(adopted, winning.sessions[&a]);
    }

    #[tokio::test]
    async fn drops_sessions_a_deletion_tombstoned() {
        let table = MemoryTable::default();
        let (deleted_session, deleted_team, kept) = (session(7, 1), session(8, 2), session(7, 3));
        table.store(
            TableKey::session(7, &deleted_session.session_id),
            Row::Deleted,
        );
        table.store(TableKey::team_day(8, "2026-10-15"), Row::Deleted);
        let store = store(table);

        let flush = store
            .keys_for_flush(
                &[deleted_session.clone(), deleted_team.clone(), kept.clone()],
                "2026-10-15",
            )
            .await
            .unwrap();
        assert_eq!(flush.sessions.keys().collect::<Vec<_>>(), vec![&kept]);
        let mut dropped = flush.dropped.clone();
        dropped.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(
            dropped,
            vec![
                (deleted_session, DropCause::Deleted),
                (deleted_team, DropCause::Deleted)
            ]
        );
        assert!(!flush.team_days.contains_key(&8));
    }
}
