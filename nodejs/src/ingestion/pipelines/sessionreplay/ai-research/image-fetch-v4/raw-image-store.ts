import { PutObjectCommand, S3Client, S3ServiceException } from '@aws-sdk/client-s3'
import { Counter } from 'prom-client'

import { sealV4Frame } from '~/ingestion/pipelines/sessionreplay/ai-research/v4-keys/frame'
import type { TeamDayKeys } from '~/ingestion/pipelines/sessionreplay/ai-research/v4-keys/team-day-keys'
import type { FetchCandidate } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/collected-urls-record'
import type { ImageFetchResult } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/image-fetcher'
import type { FetchedImageSink } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/image-sink'
import {
    InvalidImageTransportError,
    prepareFetchedImage,
} from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-scrub/image-transport'

const V4_URL_REF = /^imageurl:v4:(\d+):(\d{4}-\d{2}-\d{2}):([A-Za-z0-9_-]{22})$/

const storedImages = new Counter({
    name: 'ai_research_image_fetch_v4_stored_images_total',
    help: 'Fetched images the v4 lane handled, by outcome.',
    labelNames: ['outcome'],
})

/**
 * Stores each fetched image sealed and unscrubbed in `images-v4`, under the team day key of its reference. The v4
 * dataset is anonymized downstream, so unlike the v3 lane this sink never sends an image to the scrub lane.
 */
export class RawImageStore implements FetchedImageSink {
    constructor(
        private readonly keys: Pick<TeamDayKeys, 'get'>,
        private readonly s3: Pick<S3Client, 'send'>,
        private readonly bucket: string
    ) {}

    public async publishImage(candidate: FetchCandidate, result: ImageFetchResult): Promise<void> {
        const ref = V4_URL_REF.exec(candidate.originalRef)
        if (!ref || !result.bytes) {
            throw new Error('the v4 image store takes a fetched image with an imageurl:v4 reference')
        }
        const [, team, keyDay, hash] = ref
        let image: Buffer
        try {
            image = await prepareFetchedImage(result.bytes, result.contentType, result.contentEncoding)
        } catch (error) {
            // A body that is not the image it claims to be is handled: fetching it again returns the same body.
            if (error instanceof InvalidImageTransportError) {
                storedImages.labels('invalid').inc()
                return
            }
            throw error
        }
        const key = await this.keys.get(Number(team), keyDay)
        if (!key) {
            storedImages.labels('deleted').inc()
            return
        }
        const objectKey = `images/url/dt=${keyDay}/${team}/${hash}`
        const frame = sealV4Frame(
            key,
            { teamId: Number(team), keyDay, kind: 'image-url', codec: 'none', ref: objectKey },
            image
        )
        try {
            await this.s3.send(
                new PutObjectCommand({ Bucket: this.bucket, Key: objectKey, Body: frame, IfNoneMatch: '*' })
            )
            storedImages.labels('stored').inc()
        } catch (error) {
            // The first write of a reference stands, so a repeated fetch of it is already stored.
            if (error instanceof S3ServiceException && error.$metadata.httpStatusCode === 412) {
                storedImages.labels('already_stored').inc()
                return
            }
            throw error
        }
    }
}
