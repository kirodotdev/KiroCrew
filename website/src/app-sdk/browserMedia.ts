/**
 * The owner's media buttons for an app page (`@kirocrew/app-sdk`, level 10): play,
 * pause, seek and volume on the browser tab the app's own agent drives, without an
 * agent turn.
 *
 * The app's agent opens and works the page with the `browser` tool on the sites the
 * owner approved (`permissions.browserSites`). This hook reaches the `<video>` or
 * `<audio>` in that session's tab through `POST /api/browser/media`, an owner-only
 * route that refuses the agent's internal secret and any session that is not an
 * app's own browsing session (`dashboard/handlers/browser_media.py`). It goes
 * through the scoped client, so the app's `permissions.api` must declare
 * `/api/browser/media` (`BROWSER_MEDIA_PATH`).
 *
 * The hook draws nothing: the app lays out its own buttons and reads `media`.
 */
import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { AppApiPermissionError } from './apiError'
import { AppSdkContext } from './scopedApi'

/** The one path every member sends, for an app author to declare. */
export const BROWSER_MEDIA_PATH = '/api/browser/media'

/** The playing element's state, as the tab reported it. */
export interface BrowserMediaState {
  readonly kind: 'video' | 'audio'
  readonly paused: boolean
  readonly ended: boolean
  /** Seconds. */
  readonly currentTime: number
  /** Seconds, or null while the element does not know (a live stream, no metadata). */
  readonly duration: number | null
  /** 0 to 1. */
  readonly volume: number
  readonly muted: boolean
}

export interface BrowserMedia {
  /** The last state the tab reported, or null before the first answer. */
  readonly media: BrowserMediaState | null
  /** The last refusal's code (`no_tab`, `no_media`, `redirect_refused`, ...), or null. */
  readonly error: string | null
  /** True while a request is in flight. */
  readonly busy: boolean
  play(): Promise<void>
  pause(): Promise<void>
  /** Jump to *seconds* from the start. */
  seek(seconds: number): Promise<void>
  /** Set the volume, 0 to 1. */
  setVolume(volume: number): Promise<void>
  /** Read the state again without changing it. */
  refresh(): Promise<void>
}

type Action = 'state' | 'play' | 'pause' | 'seek' | 'volume'

interface MediaAnswer {
  ok?: boolean
  code?: string
  media?: BrowserMediaState
}

/** The media controls of *sessionKey*'s tab. With `pollMs` > 0 the state is re-read
 *  on that interval while the page is mounted (a progress bar); 0 reads it once.
 *  `onEnded` is called once each time the element turns `ended` in an answer (a
 *  poll's or a button's): the page's cue to pick the next song, for example by
 *  sending the app's own agent a message or posting to the app's own backend. It is
 *  derived from the state the page already reads, so it fires only while the page is
 *  mounted and polling. */
export function useBrowserMedia(
  sessionKey: string,
  options: { pollMs?: number; onEnded?: (media: BrowserMediaState) => void } = {},
): BrowserMedia {
  // Read off the context directly, not through the barrel's `useAppApi()`: with no
  // provider every member fails closed instead of throwing at render.
  const api = useContext(AppSdkContext)?.api ?? null
  const [media, setMedia] = useState<BrowserMediaState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // An answer for a session the page has since moved off is dropped.
  const current = useRef(sessionKey)
  current.current = sessionKey
  // The latest callback, and whether the last answer was already `ended`, so one
  // ending calls it once however many polls see it.
  const onEnded = useRef(options.onEnded)
  onEnded.current = options.onEnded
  const wasEnded = useRef(false)

  const send = useCallback(async (action: Action, value?: number) => {
    const asked = sessionKey
    if (!api) {
      setError('not_declared')
      return
    }
    setBusy(true)
    try {
      // A state read is a GET: the gateway audits mutating calls, and a poll changes nothing.
      let answer: MediaAnswer
      if (action === 'state') {
        answer = await api.get<MediaAnswer>(`${BROWSER_MEDIA_PATH}?session_key=${encodeURIComponent(asked)}`)
      } else {
        const body: Record<string, unknown> = { session_key: asked, action }
        if (value !== undefined) body.value = value
        answer = await api.post<MediaAnswer>(BROWSER_MEDIA_PATH, body)
      }
      if (current.current !== asked) return
      if (answer?.ok && answer.media) {
        setMedia(answer.media)
        setError(null)
        const ended = answer.media.ended === true
        if (ended && !wasEnded.current) onEnded.current?.(answer.media)
        wasEnded.current = ended
      } else {
        setError(answer?.code || 'browser_error')
      }
    } catch (err) {
      if (current.current !== asked) return
      if (err instanceof AppApiPermissionError) {
        setError('not_declared')
        return
      }
      let code = 'browser_error'
      try {
        code = JSON.parse((err as { body?: string })?.body || '{}').code || code
      } catch {
        // A body that is not JSON keeps the generic code.
      }
      setError(code)
    } finally {
      if (current.current === asked) setBusy(false)
    }
  }, [api, sessionKey])

  const pollMs = options.pollMs ?? 0
  useEffect(() => {
    setMedia(null)
    setError(null)
    // A song that already ended when the page mounts counts once: the page then
    // picks the next one, which is what continuous play wants.
    wasEnded.current = false
    void send('state')
    if (pollMs <= 0) return
    const timer = setInterval(() => { void send('state') }, pollMs)
    return () => clearInterval(timer)
  }, [send, pollMs])

  return {
    media,
    error,
    busy,
    play: useCallback(() => send('play'), [send]),
    pause: useCallback(() => send('pause'), [send]),
    seek: useCallback((seconds: number) => send('seek', seconds), [send]),
    setVolume: useCallback((volume: number) => send('volume', volume), [send]),
    refresh: useCallback(() => send('state'), [send]),
  }
}
