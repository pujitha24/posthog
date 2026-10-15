//! The rows of the v4 key table and their DynamoDB encoding.

use std::collections::HashMap;

use aws_sdk_dynamodb::primitives::Blob;
use aws_sdk_dynamodb::types::AttributeValue;
use chrono::{DateTime, Days, NaiveDate, Utc};
use sha2::{Digest, Sha256};

use crate::envelope::NONCE_BYTES;
use crate::seal::SealedKey;

/// v3 spreads a team's sessions over 32 partitions so one large team cannot overload one partition. v4 keeps this.
pub const SESSION_SHARDS: u32 = 32;
/// Key rows outlive the objects they seal by a day: objects expire after 7 days, and a row at the end of day 8.
const ROW_LIFETIME_DAYS: u64 = 9;

pub type Item = HashMap<String, AttributeValue>;

#[derive(Debug, Clone, thiserror::Error, PartialEq, Eq)]
pub enum RowError {
    #[error("key row has no {0}")]
    Missing(&'static str),
    #[error("key row has a malformed {0}")]
    Malformed(&'static str),
}

/// A primary key in the table.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TableKey {
    pub pk: String,
    pub sk: String,
}

impl TableKey {
    pub fn team_day(team_id: i64, key_day: &str) -> Self {
        Self {
            pk: format!("team:{team_id}"),
            sk: format!("day:{key_day}"),
        }
    }

    pub fn session(team_id: i64, session_id: &str) -> Self {
        Self {
            pk: format!("team:{team_id}:shard:{}", session_shard(session_id)),
            sk: format!("session:{session_id}"),
        }
    }

    pub fn to_item(&self) -> Item {
        HashMap::from([
            ("pk".to_owned(), AttributeValue::S(self.pk.clone())),
            ("sk".to_owned(), AttributeValue::S(self.sk.clone())),
        ])
    }

    pub fn from_item(item: &Item) -> Result<Self, RowError> {
        Ok(Self {
            pk: string(item, "pk")?,
            sk: string(item, "sk")?,
        })
    }
}

/// The v3 shard: the first 8 hex digits of `sha256(session_id)` as an integer, modulo 32.
pub fn session_shard(session_id: &str) -> u32 {
    let digest = Sha256::digest(session_id.as_bytes());
    u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]]) % SESSION_SHARDS
}

/// The UTC day of an instant, as `key_day` stores it.
pub fn key_day(at: DateTime<Utc>) -> String {
    at.format("%Y-%m-%d").to_string()
}

/// The end of day `key_day + 8`, in epoch seconds: when DynamoDB TTL may remove the row.
pub fn expires_at(key_day: &str) -> Result<i64, RowError> {
    let day = NaiveDate::parse_from_str(key_day, "%Y-%m-%d")
        .map_err(|_| RowError::Malformed("key_day"))?;
    let end = day
        .checked_add_days(Days::new(ROW_LIFETIME_DAYS))
        .ok_or(RowError::Malformed("key_day"))?;
    Ok(end
        .and_hms_opt(0, 0, 0)
        .expect("midnight exists")
        .and_utc()
        .timestamp())
}

/// What the table holds at one key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Row {
    /// A team day key, wrapped by KMS.
    TeamDay {
        team_id: i64,
        key_day: String,
        wrapped_key: Vec<u8>,
    },
    /// A session key, sealed under the team day key of `key_day`.
    Session {
        team_id: i64,
        key_day: String,
        sealed: SealedKey,
    },
    /// A deletion replaced the key material. A writer that meets one drops the data it was about to store.
    Deleted,
}

impl Row {
    /// The full item for a conditional put. `key_day` sets the row's expiry.
    pub fn to_item(&self, key: &TableKey) -> Result<Item, RowError> {
        let mut item = key.to_item();
        match self {
            Row::TeamDay {
                team_id,
                key_day,
                wrapped_key,
            } => {
                insert_common(&mut item, *team_id, key_day)?;
                item.insert(
                    "wrapped_key".to_owned(),
                    AttributeValue::B(Blob::new(wrapped_key.clone())),
                );
            }
            Row::Session {
                team_id,
                key_day,
                sealed,
            } => {
                insert_common(&mut item, *team_id, key_day)?;
                item.insert(
                    "sealed_key".to_owned(),
                    AttributeValue::B(Blob::new(sealed.sealed.clone())),
                );
                item.insert(
                    "key_nonce".to_owned(),
                    AttributeValue::B(Blob::new(sealed.nonce.to_vec())),
                );
            }
            Row::Deleted => {
                item.insert("deleted".to_owned(), AttributeValue::Bool(true));
            }
        }
        Ok(item)
    }

    pub fn from_item(item: &Item) -> Result<Self, RowError> {
        if matches!(item.get("deleted"), Some(AttributeValue::Bool(true))) {
            return Ok(Row::Deleted);
        }
        let team_id = string_number(item, "team_id")?;
        let key_day = string(item, "key_day")?;
        if let Some(wrapped) = item.get("wrapped_key") {
            return Ok(Row::TeamDay {
                team_id,
                key_day,
                wrapped_key: blob(wrapped, "wrapped_key")?,
            });
        }
        let sealed = blob(
            item.get("sealed_key")
                .ok_or(RowError::Missing("sealed_key"))?,
            "sealed_key",
        )?;
        let nonce = blob(
            item.get("key_nonce")
                .ok_or(RowError::Missing("key_nonce"))?,
            "key_nonce",
        )?;
        let nonce: [u8; NONCE_BYTES] = nonce
            .try_into()
            .map_err(|_| RowError::Malformed("key_nonce"))?;
        Ok(Row::Session {
            team_id,
            key_day,
            sealed: SealedKey { sealed, nonce },
        })
    }
}

fn insert_common(item: &mut Item, team_id: i64, key_day: &str) -> Result<(), RowError> {
    item.insert("team_id".to_owned(), AttributeValue::N(team_id.to_string()));
    item.insert("key_day".to_owned(), AttributeValue::S(key_day.to_owned()));
    item.insert(
        "expires_at".to_owned(),
        AttributeValue::N(expires_at(key_day)?.to_string()),
    );
    Ok(())
}

fn string(item: &Item, name: &'static str) -> Result<String, RowError> {
    match item.get(name) {
        Some(AttributeValue::S(value)) => Ok(value.clone()),
        Some(_) => Err(RowError::Malformed(name)),
        None => Err(RowError::Missing(name)),
    }
}

fn string_number(item: &Item, name: &'static str) -> Result<i64, RowError> {
    match item.get(name) {
        Some(AttributeValue::N(value)) => value.parse().map_err(|_| RowError::Malformed(name)),
        Some(_) => Err(RowError::Malformed(name)),
        None => Err(RowError::Missing(name)),
    }
}

fn blob(value: &AttributeValue, name: &'static str) -> Result<Vec<u8>, RowError> {
    match value {
        AttributeValue::B(blob) => Ok(blob.as_ref().to_vec()),
        _ => Err(RowError::Malformed(name)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_sessions_on_the_shards_the_v3_deletion_code_reads() {
        // Shard numbers from the Node `sessionShard`, which the Python deletion store mirrors.
        let cases = [
            ("019a2f3e-8c00-7000-8000-000000000007", 9),
            ("019a2f3e-8c00-7000-8000-000000000008", 11),
            ("00000000-0000-7000-8000-000000000000", 20),
            ("019a2f3e-8c00-7000-8000-00000000ffff", 31),
        ];
        for (session_id, shard) in cases {
            assert_eq!(
                TableKey::session(7, session_id).pk,
                format!("team:7:shard:{shard}")
            );
        }
    }

    #[test]
    fn rows_round_trip_and_expire_at_the_end_of_day_eight() {
        let session = TableKey::session(7, "019a2f3e-8c00-7000-8000-000000000007");
        let rows = [
            Row::TeamDay {
                team_id: 7,
                key_day: "2026-10-15".to_owned(),
                wrapped_key: vec![1, 2, 3],
            },
            Row::Session {
                team_id: 7,
                key_day: "2026-10-15".to_owned(),
                sealed: SealedKey {
                    sealed: vec![4; 48],
                    nonce: [5; NONCE_BYTES],
                },
            },
            Row::Deleted,
        ];
        for row in rows {
            let item = row.to_item(&session).unwrap();
            assert_eq!(Row::from_item(&item).unwrap(), row);
            assert_eq!(TableKey::from_item(&item).unwrap(), session);
        }
        let item = rows_with_expiry();
        // 2026-10-24T00:00:00Z
        assert_eq!(
            item.get("expires_at"),
            Some(&AttributeValue::N("1792800000".to_owned()))
        );
        assert_eq!(
            Row::from_item(&session.to_item()),
            Err(RowError::Missing("team_id"))
        );
    }

    fn rows_with_expiry() -> Item {
        Row::TeamDay {
            team_id: 7,
            key_day: "2026-10-15".to_owned(),
            wrapped_key: vec![1],
        }
        .to_item(&TableKey::team_day(7, "2026-10-15"))
        .unwrap()
    }
}
