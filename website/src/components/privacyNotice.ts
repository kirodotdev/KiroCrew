import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
// Leaf modules, not `../api/client`: most tests mock that module with a bare
// factory, and the second read below must not depend on exports they leave out.
import { ApiError } from '../api/apiError'
import { pendingRefresh } from '../api/refreshOnce'
import type { BeaconStatus } from './PrivacyDisclosure'

/**
 * Version of the first-run Privacy chapter's notice text.
 *
 * Stored server-side in `dashboard.privacy_ack_version` when the user passes the
 * chapter after reading the text that says local recording is on. A user whose
 * stored version is lower sees the chapter once more at the next dashboard load,
 * only while local recording is on, so bump this whenever the notice says
 * something new. The backend keeps the same number as
 * `beacon.PRIVACY_NOTICE_VERSION`; a backend test pins the two together.
 */
export const PRIVACY_NOTICE_VERSION = 2

/** GET /api/telemetry/collection, the fields the notice reads. */
interface CollectionStatus {
  enabled?: boolean
  /** A `config.local.json` entry decides the switch. */
  overlay_override?: boolean
  env_pinned?: boolean
  env_var?: string
  /** Recording would also send metrics to a configured destination. */
  otlp_configured?: boolean
}

/**
 * Why usage data is off. `config` is a choice the user can change in Settings;
 * the rest are pins this screen cannot lift.
 */
export type UsageOffReason = 'governance' | 'do_not_track' | 'env' | 'config'

export interface UsageDataState {
  /** `loading` until both status routes answered; `error` when either failed. */
  status: 'loading' | 'ready' | 'error'
  /** Local recording or the heartbeat is on, as the gateway resolves it. */
  on: boolean
  /** Local metric recording is on (its EFFECTIVE state). */
  recordingOn: boolean
  /** The daily heartbeat is on and not pinned off. */
  heartbeatOn: boolean
  /** Local recording is on AND sends metrics to a configured destination. */
  exportOn: boolean
  /** Set only when `on` is false. */
  offReason: UsageOffReason | null
  /** The env var to name when `offReason` is `env`. */
  envVar: string
  /** Off, and a `config.local.json` entry holds a switch off (not the user's own setting). */
  overlayOff?: boolean
}

/**
 * One status read, tried once more when the gateway refused it because the
 * dashboard sign-in had run out. On that refusal the client starts renewing the
 * sign-in and hands the refusal back unchanged (`checkSessionExpired`), so the
 * first read after a lapse fails while the same read a moment later answers.
 * The second read waits for a renewal that is still running. A renewal that
 * failed, or a second read that fails, leaves the read failed.
 */
async function readAfterRenewal<T>(read: () => Promise<T>): Promise<T> {
  try {
    return await read()
  } catch (e) {
    // A proxy's own sign-in page sets `authRequired` too, and no renewal clears it.
    if (!(e instanceof ApiError && e.authRequired && !e.edgeChallenge)) throw e
    const renewal = pendingRefresh()
    if (renewal && !(await renewal).ok) throw e
    return read()
  }
}

/**
 * Whether any usage data is on, read from the two routes Settings > Privacy
 * already uses, under the same query keys, so the chapter, the panel and this
 * answer share one cache entry each and cannot disagree.
 *
 * "On" means the gateway would record or send something by the user's own
 * settings: local recording's EFFECTIVE state, or the heartbeat switch on and not
 * pinned off by an env var or policy. A CI host or a non-default data home pause
 * the heartbeat without being anyone's choice, so they do not count as off.
 */
export function useUsageDataState(enabled: boolean): UsageDataState {
  const beaconQ = useQuery<BeaconStatus>({
    queryKey: ['beaconStatus'],
    queryFn: () => readAfterRenewal(() => api.beaconStatus()),
    enabled,
    retry: false,
  })
  const collectionQ = useQuery<CollectionStatus>({
    queryKey: ['telemetryCollection'],
    queryFn: () => readAfterRenewal(() => api.collectionStatus()),
    enabled,
    retry: false,
  })
  if (beaconQ.isError || collectionQ.isError) {
    return { status: 'error', on: false, recordingOn: false, heartbeatOn: false, exportOn: false, offReason: null, envVar: '' }
  }
  const beacon = beaconQ.data
  const collection = collectionQ.data
  if (!beacon || !collection) {
    return { status: 'loading', on: false, recordingOn: false, heartbeatOn: false, exportOn: false, offReason: null, envVar: '' }
  }
  const recordingOn = !!collection.enabled
  const heartbeatOn = !!beacon.enabled && !beacon.env_override && !beacon.governance_override
  const exportOn = recordingOn && !!collection.otlp_configured
  if (recordingOn || heartbeatOn) {
    return { status: 'ready', on: true, recordingOn, heartbeatOn, exportOn, offReason: null, envVar: '' }
  }
  const envVar = (beacon.env_override ? beacon.env_var : collection.env_pinned ? collection.env_var : '') ?? ''
  let offReason: UsageOffReason = 'config'
  if (beacon.governance_override) offReason = 'governance'
  else if (envVar === 'DO_NOT_TRACK') offReason = 'do_not_track'
  else if (envVar) offReason = 'env'
  const overlayOff = !!(beacon.overlay_override || collection.overlay_override)
  return { status: 'ready', on: false, recordingOn: false, heartbeatOn: false, exportOn: false, offReason, envVar, overlayOff }
}
