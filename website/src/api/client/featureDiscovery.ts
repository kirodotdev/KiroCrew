/**
 * What the dashboard surfaces unprompted: welcome-screen suggestions, the
 * startup feature videos, and tips.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { ClientTransport } from './transport'

/** Category of a welcome-screen suggestion; drives its icon tile. */
export type SuggestionKind = 'code' | 'review' | 'ops' | 'tasks' | 'write' | 'research' | 'schedule' | 'general'

/** One `/api/suggestions` item. `kind` is a string on the wire, so an unknown value is possible. */
export type SuggestionItem = string | { text: string; kind?: string }

/** One feature-intro clip, as GET /api/feature-videos/next reports it.
 *
 *  `src` and `poster` are whole URLs the BACKEND authored, and the only correct
 *  use of them is to play them verbatim. A clip cached on disk is named by a
 *  same-origin path under `/feature-videos/<release>/`; a clip still on the CDN
 *  is named by an absolute `https://` URL. Which of the two it is is stated in
 *  `source`, so the client never has to guess from the string and never composes
 *  a URL of its own — that is what keeps a config value from pointing the player
 *  at a third-party host. */
export interface FeatureVideo {
  id: string
  /** Which dashboard feature the clip introduces — the per-feature key the
   *  backend dedupes on, so a verdict survives the clip being re-cut under a
   *  new id. */
  feature: string
  title: string
  description: string
  src: string
  poster: string
  duration_s: number
  /** Docs FILENAME, as the backend's catalog stores it (`"feature-tips.md"`) --
   *  not a URL, and unlike `tipsNext` no resolved `doc_link` ships beside it.
   *  Resolve it with `tipDocHref` from `utils/docsLink`, which validates the
   *  filename shape and returns the public docs URL. */
  doc?: string
  /** Where the bytes are RIGHT NOW: `'local'` means the release folder on this
   *  machine has the file, `'remote'` means the player will stream it from the
   *  CDN. It changes what the clip costs to open, not what it is -- so it drives
   *  `preload` and the streaming hint, and nothing else.
   *
   *  Optional because a gateway that predates the remote catalog sends no such
   *  field, and an absent answer means the clip is the local kind it has always
   *  been. The release a clip belongs to is NOT here: the settings panel reads
   *  that from `FeatureVideoStatus`, and a second copy on the clip had no
   *  reader. */
  source?: 'local' | 'remote'
}

/** GET /api/feature-videos/next.
 *
 *  `video: null` is the steady state, not an error — it is what the endpoint
 *  returns once every clip has been seen or dismissed, which for most launches
 *  is always. `enabled` is the operator kill switch, reported separately so a
 *  disabled install still answers 200 rather than making the client read a
 *  failure as a policy. */
export interface FeatureVideoNext {
  video: FeatureVideo | null
  enabled: boolean
  /** May this install pull clip bytes over the network at all? Reported beside
   *  the clip because it is what makes a `'remote'` offer playable: with
   *  downloads off there is no route to the bytes, so the modal treats a remote
   *  clip as unshowable rather than opening a player that cannot fill.
   *  Optional on the wire so a gateway that predates it reads as OFF -- the
   *  fail-closed direction. */
  download_enabled?: boolean
}

/** GET /api/feature-videos/probe.
 *
 *  Server-side reachability for one clip, by id. It replaces a client-side HEAD,
 *  which could only ever work for a same-origin path: a CDN URL answers a
 *  cross-origin HEAD without CORS headers, so the browser reports a network
 *  failure and an entirely healthy clip reads as missing. The server has no such
 *  restriction, and it is also the side that knows whether the file is in the
 *  release folder. */
export interface FeatureVideoProbe {
  ok: boolean
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
}

export function createFeatureDiscoveryEndpoints({ get, post, j, jNullable }: ClientTransport) {
  const suggestions = {
    // Items are a bare string (legacy / cached payloads) or `{ text, kind }`.
    suggestions: (force?: boolean) => fetch(relocateRequestUrl(`/api/suggestions${force ? '?force=1' : ''}`)).then(j) as Promise<{ suggestions: SuggestionItem[]; generated_at: number; stale: boolean }>,
  }

  const featureVideos = {
    // Feature intro videos (startup). This GET carries METADATA only — the clip
    // itself is fetched by the <video> element, and only after the modal opens,
    // so a launch that shows nothing costs one small JSON round trip.
    //
    // `sessionKey` MUST carry the active slot's key (`dashboard:<slot>`). Both routes
    // call the server's `_is_restricted_session`, and that guard treats the shared
    // `dashboard:ui` default as NOT restricted (`_shared.py:1668`) -- so omitting the
    // key makes the server's own incognito/temporary check unreachable, and the only
    // thing left standing between a session that keeps nothing and a PERMANENT verdict
    // is the dashboard's client-side gate. Same cooperative-honesty contract as
    // `mobileLoginLink` (`./remoteAccess`).
    featureVideoNext: (sessionKey?: string) =>
      get('/api/feature-videos/next', sessionKey).then(j) as Promise<FeatureVideoNext>,
    /** Permanent per-video verdict, not a snooze: `seen` retires the clip on
     *  completion or an explicit acknowledgement, `dismissed` retires it on a
     *  close, and the backend never offers that video again after either. */
    featureVideoFeedback: (id: string, status: 'seen' | 'dismissed', sessionKey?: string) =>
      post('/api/feature-videos/feedback', { id, status }, sessionKey).then(j) as Promise<{ ok: true }>,
    /** Is this clip's file actually reachable? Asked of the SERVER, because the
     *  client cannot ask it: a remote clip lives on another origin, where a HEAD
     *  from the page is refused for want of CORS headers and a healthy clip is
     *  indistinguishable from a missing one. */
    featureVideoProbe: (id: string, sessionKey?: string) =>
      get('/api/feature-videos/probe?id=' + encodeURIComponent(id), sessionKey)
        .then(j) as Promise<FeatureVideoProbe>,
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

  return { suggestions, featureVideos, tips }
}
