import {
    AttributeValue,
    ConditionalCheckFailedException,
    DynamoDBClient,
    GetItemCommand,
    PutItemCommand,
} from '@aws-sdk/client-dynamodb'
import { DecryptCommand, GenerateDataKeyCommand, KMSClient } from '@aws-sdk/client-kms'
import { LRUCache } from 'lru-cache'

const DAY_MS = 24 * 60 * 60 * 1000
/** Key rows outlive the objects they seal by a day: objects expire after 7 days, a row at the end of day 8. */
const ROW_LIFETIME_DAYS = 9
const KMS_TIMEOUT_MS = 5000
/** A tombstone written after this is seen within this time, as the v3 row cache bounds it. */
const CACHE_LIFETIME_MS = 300_000

type Item = Record<string, AttributeValue>

/** The KMS encryption context of a team day key. The Rust recorder and the key policy use the same one. */
export function teamDayWrappingContext(teamId: number, keyDay: string): Record<string, string> {
    return { key_day: keyDay, purpose: 'ai-research-v4', team_id: String(teamId) }
}

export function teamDayRowKey(teamId: number, keyDay: string): Item {
    return { pk: { S: `team:${teamId}` }, sk: { S: `day:${keyDay}` } }
}

/** The end of day `keyDay + 8`, in epoch seconds: when DynamoDB TTL may remove the row. */
export function rowExpiresAt(keyDay: string): number {
    const start = Date.parse(`${keyDay}T00:00:00Z`)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(keyDay) || Number.isNaN(start)) {
        throw new Error('a v4 key day must be YYYY-MM-DD')
    }
    return (start + ROW_LIFETIME_DAYS * DAY_MS) / 1000
}

/**
 * The v4 team day keys in `ai-research-replay-v4-keys`: the stored key for a team and day, or a new one stored with a
 * conditional put. Resolves to null when a deletion tombstoned the team's day, so the caller stores nothing.
 */
export class TeamDayKeys {
    private readonly cache = new LRUCache<string, { key: Buffer | null }>({ max: 10_000, ttl: CACHE_LIFETIME_MS })
    private readonly pending = new Map<string, Promise<Buffer | null>>()

    constructor(
        private readonly dynamo: Pick<DynamoDBClient, 'send'>,
        private readonly kms: Pick<KMSClient, 'send'>,
        private readonly tableName: string,
        private readonly kmsKeyArn: string
    ) {}

    public async get(teamId: number, keyDay: string): Promise<Buffer | null> {
        const id = `${teamId}:${keyDay}`
        const cached = this.cache.get(id)
        if (cached) {
            return cached.key
        }
        // Concurrent requests for one team and day share a read, so a burst of images makes one KMS call.
        let pending = this.pending.get(id)
        if (!pending) {
            pending = this.resolve(teamId, keyDay).then((key) => {
                this.cache.set(id, { key })
                return key
            })
            this.pending.set(id, pending)
        }
        try {
            return await pending
        } finally {
            this.pending.delete(id)
        }
    }

    private async resolve(teamId: number, keyDay: string): Promise<Buffer | null> {
        const stored = await this.dynamo.send(
            new GetItemCommand({ TableName: this.tableName, Key: teamDayRowKey(teamId, keyDay), ConsistentRead: true })
        )
        if (stored.Item) {
            return this.unwrapRow(teamId, keyDay, stored.Item)
        }
        const context = teamDayWrappingContext(teamId, keyDay)
        const generated = await this.kms.send(
            new GenerateDataKeyCommand({ KeyId: this.kmsKeyArn, KeySpec: 'AES_256', EncryptionContext: context }),
            { abortSignal: AbortSignal.timeout(KMS_TIMEOUT_MS) }
        )
        if (generated.Plaintext?.length !== 32 || !generated.CiphertextBlob?.length) {
            throw new Error('KMS returned an invalid v4 data key')
        }
        try {
            await this.dynamo.send(
                new PutItemCommand({
                    TableName: this.tableName,
                    Item: {
                        ...teamDayRowKey(teamId, keyDay),
                        team_id: { N: String(teamId) },
                        key_day: { S: keyDay },
                        wrapped_key: { B: generated.CiphertextBlob },
                        expires_at: { N: String(rowExpiresAt(keyDay)) },
                    },
                    ConditionExpression: 'attribute_not_exists(pk)',
                    ReturnValuesOnConditionCheckFailure: 'ALL_OLD',
                })
            )
            return Buffer.from(generated.Plaintext)
        } catch (error) {
            if (!(error instanceof ConditionalCheckFailedException)) {
                throw error
            }
            // Another writer stored the day's key first: adopt it, or stop if it is a tombstone.
            const winner =
                error.Item ??
                (
                    await this.dynamo.send(
                        new GetItemCommand({
                            TableName: this.tableName,
                            Key: teamDayRowKey(teamId, keyDay),
                            ConsistentRead: true,
                        })
                    )
                ).Item
            if (!winner) {
                throw new Error('a v4 key put lost to a row that is gone')
            }
            return this.unwrapRow(teamId, keyDay, winner)
        }
    }

    private async unwrapRow(teamId: number, keyDay: string, item: Item): Promise<Buffer | null> {
        if (item.deleted?.BOOL === true) {
            return null
        }
        const wrapped = item.wrapped_key?.B
        if (!wrapped?.length) {
            throw new Error('a v4 team day row has no wrapped key')
        }
        const decrypted = await this.kms.send(
            new DecryptCommand({
                KeyId: this.kmsKeyArn,
                CiphertextBlob: wrapped,
                EncryptionContext: teamDayWrappingContext(teamId, keyDay),
            }),
            { abortSignal: AbortSignal.timeout(KMS_TIMEOUT_MS) }
        )
        if (decrypted.Plaintext?.length !== 32) {
            throw new Error('KMS returned an invalid v4 plaintext key')
        }
        return Buffer.from(decrypted.Plaintext)
    }
}
