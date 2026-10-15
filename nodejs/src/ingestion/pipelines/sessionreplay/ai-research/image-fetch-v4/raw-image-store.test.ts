import { PutObjectCommand, S3ServiceException } from '@aws-sdk/client-s3'
import { createDecipheriv } from 'node:crypto'

import { parseJSON } from '~/common/utils/json-parse'
import type { FetchCandidate } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/collected-urls-record'
import type { ImageFetchResult } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/image-fetcher'

import { RawImageStore } from './raw-image-store'

const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from('png body')])
const REF = 'imageurl:v4:7:2026-10-15:abcdefghijklmnopqrstuv'
const KEY = Buffer.alloc(32, 9)

function open(frame: Buffer): { aad: any; body: Buffer } {
    const length = frame.readUInt16BE(6)
    const aad = frame.subarray(8, 8 + length)
    const nonce = frame.subarray(8 + length, 20 + length)
    const sealed = frame.subarray(20 + length)
    const decipher = createDecipheriv('aes-256-gcm', KEY, nonce, { authTagLength: 16 })
    decipher.setAAD(aad)
    decipher.setAuthTag(sealed.subarray(sealed.length - 16))
    const body = Buffer.concat([decipher.update(sealed.subarray(0, sealed.length - 16)), decipher.final()])
    return { aad: parseJSON(aad.toString()), body }
}

function store(key: Buffer | null, putError?: Error) {
    const puts: PutObjectCommand[] = []
    const s3 = {
        send: jest.fn((command: PutObjectCommand) => {
            puts.push(command)
            return putError ? Promise.reject(putError) : Promise.resolve({})
        }),
    }
    return { sink: new RawImageStore({ get: () => Promise.resolve(key) }, s3 as any, 'images-v4'), puts }
}

const candidate = (ref = REF) => ({ originalRef: ref }) as FetchCandidate
const fetched = (bytes = PNG, contentType = 'image/png') => ({ outcome: 'ok', bytes, contentType }) as ImageFetchResult

describe('RawImageStore', () => {
    it('stores the image sealed under its team day key, at a key derived from its reference', async () => {
        const { sink, puts } = store(KEY)
        await sink.publishImage(candidate(), fetched())

        expect(puts).toHaveLength(1)
        const { Bucket, Key, IfNoneMatch, Body } = puts[0].input
        expect([Bucket, Key, IfNoneMatch]).toEqual([
            'images-v4',
            'images/url/dt=2026-10-15/7/abcdefghijklmnopqrstuv',
            '*',
        ])
        const { aad, body } = open(Body as Buffer)
        expect(body).toEqual(PNG)
        expect(aad).toEqual({
            v: 4,
            context: { teamId: 7, keyDay: '2026-10-15', kind: 'image-url', codec: 'none', ref: Key },
        })
    })

    it.each([
        ['a tombstoned team day', null, fetched(), undefined],
        ['a body that is not the image it claims to be', KEY, fetched(Buffer.from('<html>')), undefined],
        [
            'a reference stored before',
            KEY,
            fetched(),
            new S3ServiceException({
                name: 'PreconditionFailed',
                $fault: 'client',
                $metadata: { httpStatusCode: 412 },
            }),
        ],
    ])('resolves without a new object for %s', async (_name, key, result, putError) => {
        const { sink, puts } = store(key, putError)
        await expect(sink.publishImage(candidate(), result)).resolves.toBeUndefined()
        expect(puts.length).toBe(putError ? 1 : 0)
    })

    it('refuses a reference from another dataset', async () => {
        const { sink } = store(KEY)
        await expect(
            sink.publishImage(candidate('imageurl:v3:7:2026-10:abcdefghijklmnopqrstuv'), fetched())
        ).rejects.toThrow()
    })
})
