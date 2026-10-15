import {
    KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4,
    KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_1H,
    KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_1M,
    KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_10M,
} from '~/common/config/kafka-topics'
import type { ImageFetchLane } from '~/servers/ingestion-session-replay-ml-image-fetch-server'

/** The v4 lane has its own frontier and delay tiers, so its jobs never mix with the v3 lane's. */
export const V4_IMAGE_FETCH_LANE: ImageFetchLane = {
    frontier: KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4,
    retry1m: KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_1M,
    retry10m: KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_10M,
    retry1h: KAFKA_SESSION_REPLAY_IMAGE_FETCH_V4_RETRY_1H,
}
