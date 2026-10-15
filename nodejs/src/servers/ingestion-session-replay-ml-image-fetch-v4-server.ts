import { DynamoDBClient } from '@aws-sdk/client-dynamodb'
import { KMSClient } from '@aws-sdk/client-kms'
import { S3Client } from '@aws-sdk/client-s3'

import { V4_IMAGE_FETCH_LANE } from '~/ingestion/pipelines/sessionreplay/ai-research/image-fetch-v4/lane'
import { RawImageStore } from '~/ingestion/pipelines/sessionreplay/ai-research/image-fetch-v4/raw-image-store'
import { TeamDayKeys } from '~/ingestion/pipelines/sessionreplay/ai-research/v4-keys/team-day-keys'
import { FrontierPublisher } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/frontier-publisher'
import type { FetchedImageSink } from '~/ingestion/pipelines/sessionreplay/ml-mirror-image-fetch/image-sink'

import { IngestionSessionReplayMlImageFetchRetryServer } from './ingestion-session-replay-ml-image-fetch-retry-server'
import {
    ImageFetchLane,
    IngestionSessionReplayMlImageFetchServer,
} from './ingestion-session-replay-ml-image-fetch-server'

/**
 * The v4 image fetch lane: the v3 fetch policy over the v4 recorder's URLs, storing each image sealed and unscrubbed in
 * `images-v4`. Its consumer group, per-host limits and dry run come from the deployment's settings, as for v3.
 */
export class IngestionSessionReplayMlImageFetchV4Server extends IngestionSessionReplayMlImageFetchServer {
    protected override readonly lane: ImageFetchLane = V4_IMAGE_FETCH_LANE

    protected override imageSink(_publisher: FrontierPublisher): Promise<FetchedImageSink> {
        const {
            AI_RESEARCH_REPLAY_V4_KEY_TABLE,
            AI_RESEARCH_REPLAY_V4_KMS_KEY_ARN,
            AI_RESEARCH_REPLAY_V4_IMAGES_BUCKET,
        } = this.config
        if (
            !AI_RESEARCH_REPLAY_V4_KEY_TABLE ||
            !AI_RESEARCH_REPLAY_V4_KMS_KEY_ARN ||
            !AI_RESEARCH_REPLAY_V4_IMAGES_BUCKET
        ) {
            throw new Error(
                'the v4 image fetch lane needs AI_RESEARCH_REPLAY_V4_KEY_TABLE, AI_RESEARCH_REPLAY_V4_KMS_KEY_ARN and AI_RESEARCH_REPLAY_V4_IMAGES_BUCKET'
            )
        }
        const region = this.config.SESSION_RECORDING_V2_S3_REGION || 'us-east-1'
        const keys = new TeamDayKeys(
            new DynamoDBClient({ region, endpoint: this.config.SESSION_RECORDING_DYNAMODB_ENDPOINT, maxAttempts: 5 }),
            new KMSClient({ region, maxAttempts: 3 }),
            AI_RESEARCH_REPLAY_V4_KEY_TABLE,
            AI_RESEARCH_REPLAY_V4_KMS_KEY_ARN
        )
        return Promise.resolve(
            new RawImageStore(keys, new S3Client({ region, maxAttempts: 3 }), AI_RESEARCH_REPLAY_V4_IMAGES_BUCKET)
        )
    }
}

/** Relays the v4 lane's delay tiers back to its frontier. */
export class IngestionSessionReplayMlImageFetchV4RetryServer extends IngestionSessionReplayMlImageFetchRetryServer {
    protected override readonly lane: ImageFetchLane = V4_IMAGE_FETCH_LANE
}
