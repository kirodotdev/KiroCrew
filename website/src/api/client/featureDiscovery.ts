/**
 * What the dashboard surfaces unprompted: welcome-screen suggestions, the
 * Explore panel's feature videos, and tips.
 */

import type { ClientTransport } from './transport'

/** Category of a welcome-screen suggestion; drives its icon tile. */
export type SuggestionKind = 'code' | 'review' | 'ops' | 'tasks' | 'write' | 'research' | 'schedule' | 'general'

/** One `/api/suggestions` item. `kind` is a string on the wire, so an unknown value is possible. */
export type SuggestionItem = string | { text: string; kind?: string }

/** One feature-intro clip, as GET /api/feature-videos/library lists it.
 *
 *  `src` and `poster` are same-origin paths the BACKEND authored, and the only
 *  correct use of them is to play them verbatim. Every clip offered is on this
 *  machine already (a downloaded clip under `/feature-videos/<release>/`), so the
 *  client never composes a URL of its own -- that is what keeps a config value
 *  from pointing the player at a third-party host. */
export interface FeatureVideo {
  id: string
  /** Which dashboard feature the clip introduces. */
  feature: string
  /** Manifest data, English only, like the clip itself. */
  title: string
  description: string
  src: string
  poster: string
  duration_s: number
  /** Docs FILENAME, as the backend's catalog stores it (`"feature-tips.md"`) --
   *  not a URL. Resolve it with `tipDocHref` from `utils/docsLink`. */
  doc?: string
}

/** GET /api/feature-videos/library.
 *
 *  Every clip the Explore card may show: on disk, no `seen` / `dismissed`
 *  verdict, and not for a feature already in use. An empty list is the steady
 *  state once everything is finished, and hides the card. `enabled` is the
 *  operator kill switch; with it off the list is empty. */
export interface FeatureVideoLibrary {
  videos: FeatureVideo[]
  enabled: boolean
}

/** GET /api/feature-videos/status — the cache readout the settings panel shows. */
export interface FeatureVideoStatus {
  /** Operator kill switch for the feature as a whole. */
  enabled: boolean
  /** May clip bytes be pulled over the network. False hides the manual control:
   *  a button whose only outcome is a refusal is worse than no button.
   *
   *  Optional, and that is the FEATURE DETECT. This route already exists on a
   *  gateway that predates the cache, where it answers 200 with a different
   *  payload (`{enabled, state: {<id>: ...}}`) and none of the fields below. An
   *  absent `download_enabled` therefore means "this gateway has no cache to
   *  report", which the panel renders as no row at all -- reading it as `false`
   *  would make the row assert a download policy that does not exist. */
  download_enabled?: boolean
  /** Which versioned release folder the counts below describe. */
  release: string
  /** Clips of that release present on disk. */
  cached: number
  /** Clips of that release in the catalog. */
  total: number
  /** The clip being fetched right now, or null when nothing is in flight. */
  downloading: string | null
  /** That clip's title, which is what the panel shows. Null when nothing is in
   *  flight or the manifest gave no title; absent on a gateway that predates it. */
  downloading_title?: string | null
  /** The permanent per-video verdicts, `{<id>: {status, ts}}`. Empty for a
   *  session that withholds reads. */
  state?: Record<string, { status: 'seen' | 'dismissed'; ts: number }>
}

/** A new-feature row's starter task (RFC "Milestones for New-User Feature
 *  Discovery", 4.1). Its text is not here: it lives in the i18n catalog under
 *  `milestone.<id>.starter_prompt`, so nothing the server sends reaches a model. */
export interface MilestoneStarter {
  kind: 'prompt' | 'route'
  /** `send` only for the reviewed low-cost rows; anything else reads as prefill. */
  send: 'prefill' | 'send'
  route?: string
  agent?: string
  crew_mode?: boolean
}

export interface MilestoneFeatureItem {
  id: string
  state: 'done' | 'open' | 'skipped' | 'unavailable'
  done_at: number | null
  source: 'event' | 'backfill' | 'manual' | null
  video: FeatureVideo | null
  starter: MilestoneStarter | null
  doc?: string
}

export interface MilestoneUsageItem {
  id: string
  value: number
  tiers: number[]
  tiers_reached_at: Record<string, number>
}

export type MilestoneGroup =
  | { id: 'features'; items: MilestoneFeatureItem[] }
  | { id: 'sessions' | 'turns' | 'streak' | 'concurrent'; items: MilestoneUsageItem[] }

/** GET /api/milestones. A restricted session or a switched-off install
 *  answers `{ enabled: false }` alone. */
export interface Milestones {
  enabled: boolean
  features?: { done: number; total: number }
  streak?: { current: number; best: number }
  groups?: MilestoneGroup[]
  recommended?: string[]
  notices?: string[]
}

export type MilestoneEventAction = 'starter_clicked' | 'notice_shown' | 'panel_opened'

/** Where an event happened, as the server's metric labels it. */
export type MilestoneSurface = 'empty_state' | 'panel'

export function createFeatureDiscoveryEndpoints({ get, post, j, jfetch: fetch, jNullable }: ClientTransport) {
  // Milestones. Every route carries the active slot's key for the same reason
  // the feature-video routes do: the server's restricted-session guard must see
  // the real session, not the shared default.
  const milestones = {
    milestones: (sessionKey?: string) => get('/api/milestones', sessionKey).then(j) as Promise<Milestones>,
    /** `surface` names where a starter was clicked; the server defaults it to `empty_state`. */
    milestoneEvent: (id: string, action: MilestoneEventAction, sessionKey?: string, surface?: MilestoneSurface) =>
      post('/api/milestones/event', surface ? { id, action, surface } : { id, action }, sessionKey).then(j) as Promise<unknown>,
    milestoneMarkDone: (id: string, sessionKey?: string) =>
      post('/api/milestones/mark-done', { id }, sessionKey).then(j) as Promise<unknown>,
  }

  const suggestions = {
    // Items are a bare string (legacy / cached payloads) or `{ text, kind }`.
    suggestions: (force?: boolean) => fetch(`/api/suggestions${force ? '?force=1' : ''}`).then(j) as Promise<{ suggestions: SuggestionItem[]; generated_at: number; stale: boolean }>,
  }

  const featureVideos = {
    // Feature intro videos (the Explore card). This GET carries METADATA only:
    // the clip itself is fetched by the <video> element, and only after Watch.
    //
    // `sessionKey` MUST carry the active slot's key (`dashboard:<slot>`). Both routes
    // call the server's `_is_restricted_session`, and that guard treats the shared
    // `dashboard:ui` default as NOT restricted (`_shared.py:1668`) -- so omitting the
    // key makes the server's own incognito/temporary check unreachable, and the only
    // thing left standing between a session that keeps nothing and a PERMANENT verdict
    // is the dashboard's client-side gate. Same cooperative-honesty contract as
    // `mobileLoginLink` (`./remoteAccess`).
    featureVideoLibrary: (sessionKey?: string) =>
      get('/api/feature-videos/library', sessionKey).then(j) as Promise<FeatureVideoLibrary>,
    /** Permanent per-video verdict, not a snooze: `seen` retires the clip on
     *  completion or an explicit acknowledgement, `dismissed` retires it on a
     *  close, and the backend never offers that video again after either. */
    featureVideoFeedback: (id: string, status: 'seen' | 'dismissed', sessionKey?: string) =>
      post('/api/feature-videos/feedback', { id, status }, sessionKey).then(j) as Promise<{ ok: true }>,
    /** Cache readout for the settings panel.
     *
     *  `sessionKey` MUST carry the active slot's key, for the same reason the two
     *  routes above do and with a sharper edge: this route's read gate is
     *  `_blocks_reads_session`, which returns "not restricted" for a MISSING key
     *  and for the shared `dashboard:ui` placeholder alike. A request that omits it
     *  is therefore served the permanent engagement history even from a temporary
     *  session, whose whole contract is that reads are withheld -- and react-query
     *  would cache it. Naming the real slot is what makes the server's own gate
     *  reachable. */
    featureVideoStatus: (sessionKey?: string) =>
      get('/api/feature-videos/status', sessionKey).then(j) as Promise<FeatureVideoStatus>,
    /** Start fetching every clip of the current release now, rather than waiting
     *  for the background pass. Returns as soon as the work is QUEUED -- the
     *  progress is read back from `featureVideoStatus`.
     *
     *  Carries the session key for the same reason: it is the write half of the
     *  same feature behind the same gate, and a pair where only one side names the
     *  session is the shape that leaves the other side open. */
    featureVideoFetchAll: (sessionKey?: string) =>
      post('/api/feature-videos/fetch-all', undefined, sessionKey).then(j) as Promise<{ ok: true }>,
  }

  const tips = {
    // Tips
    tipsNext: () => get('/api/tips/next').then(jNullable) as Promise<{ tip: { id: string; feature: string; title: string; body: string; why: string; doc: string; doc_link?: string; cta_prompt: string; action?: { kind: 'route'; label: string; route: string } | null } | null; glow: boolean } | null>,
    tipsStatus: () => get('/api/tips/status').then(j) as Promise<{ enabled_config: boolean; opted_out: boolean; cadence_hours: number }>,
    tipsFeedback: (id: string, action: 'shown' | 'ack' | 'dismiss' | 'snooze' | 'helpful' | 'optout' | 'optin') => post('/api/tips/feedback', { id, action }).then(j),
  }

  return { suggestions, featureVideos, tips, milestones }
}
