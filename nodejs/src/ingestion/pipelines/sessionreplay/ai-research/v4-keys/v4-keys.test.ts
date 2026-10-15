import { ConditionalCheckFailedException, GetItemCommand, PutItemCommand } from '@aws-sdk/client-dynamodb'
import { DecryptCommand, GenerateDataKeyCommand } from '@aws-sdk/client-kms'

import vector from '~/ingestion/pipelines/sessionreplay/ml-mirror/keys/encryption-vector-v4.json'

import { V4FrameContext, sealV4Frame } from './frame'
import { TeamDayKeys, rowExpiresAt, teamDayWrappingContext } from './team-day-keys'

const base64 = (value: string): Buffer => Buffer.from(value, 'base64')

describe('v4 keys', () => {
    it.each([
        [
            'an image under the team day key',
            vector.teamDayKey,
            vector.image.context,
            vector.image.bytes,
            vector.image.nonce,
            vector.image.frame,
        ],
        [
            'a block under the session key',
            vector.sessionKey,
            vector.block.context,
            vector.block.body,
            vector.block.nonce,
            vector.block.frame,
        ],
    ])('seals %s exactly as the Rust recorder does', (_name, key, context, body, nonce, frame) => {
        // The Rust crate pins the same file, so the two writers cannot drift apart.
        expect(sealV4Frame(base64(key), context as V4FrameContext, base64(body), base64(nonce))).toEqual(base64(frame))
    })

    const TEAM = 7
    const DAY = '2026-10-15'
    const wrapped = Buffer.from('wrapped')
    const stored = { pk: { S: 'team:7' }, sk: { S: `day:${DAY}` }, wrapped_key: { B: wrapped } }

    function fakes(row: Record<string, any> | undefined, lostTo?: Record<string, any>) {
        const puts: PutItemCommand[] = []
        const dynamo = {
            send: jest.fn((command: unknown) => {
                if (command instanceof GetItemCommand) {
                    return Promise.resolve({ Item: row })
                }
                if (command instanceof PutItemCommand) {
                    puts.push(command)
                    if (lostTo) {
                        return Promise.reject(
                            new ConditionalCheckFailedException({ message: 'exists', $metadata: {}, Item: lostTo })
                        )
                    }
                    return Promise.resolve({})
                }
                throw new Error('unexpected command')
            }),
        }
        const kms = {
            send: jest.fn((command: unknown) => {
                if (command instanceof GenerateDataKeyCommand) {
                    return Promise.resolve({ Plaintext: Buffer.alloc(32, 1), CiphertextBlob: Buffer.from('new') })
                }
                if (command instanceof DecryptCommand) {
                    expect(command.input.EncryptionContext).toEqual(teamDayWrappingContext(TEAM, DAY))
                    return Promise.resolve({ Plaintext: Buffer.alloc(32, 2) })
                }
                throw new Error('unexpected command')
            }),
        }
        return { keys: new TeamDayKeys(dynamo, kms, 'table', 'arn'), puts }
    }

    it.each([
        ['unwraps a stored key', stored, undefined, Buffer.alloc(32, 2), 0],
        ['stores a new key for a new day', undefined, undefined, Buffer.alloc(32, 1), 1],
        ['adopts the key another writer stored first', undefined, stored, Buffer.alloc(32, 2), 1],
        [
            'stores nothing for a tombstoned day',
            { ...stored, wrapped_key: undefined, deleted: { BOOL: true } },
            undefined,
            null,
            0,
        ],
    ])('%s', async (_name, row, lostTo, expected, putCount) => {
        const { keys, puts } = fakes(row, lostTo)
        expect(await keys.get(TEAM, DAY)).toEqual(expected)
        expect(puts).toHaveLength(putCount)
        if (putCount > 0) {
            expect(puts[0].input.Item?.expires_at).toEqual({ N: String(rowExpiresAt(DAY)) })
            expect(puts[0].input.ConditionExpression).toBe('attribute_not_exists(pk)')
        }
    })
})
