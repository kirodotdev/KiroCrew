import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { api } from '../../api/client'
import type { FeatureVideo, FeatureVideoLibrary } from '../../api/client/featureDiscovery'
import { useAppSelector } from '../../store'
import Glass from '../Glass'
import ErrorNotice from '../ErrorNotice'
import { recordError } from '../../utils/errorReport'

/** Share of the clip after which it counts as watched. */
const SEEN_AT = 0.8

/** How often an open Explore with no clip listed asks again. */
const LIBRARY_POLL_MS = 60_000

/** The clip this page session drew. Module state, so closing and reopening
 *  Explore shows the same clip until it is finished; a reload draws again. */
let sessionPick: string | null = null

/** Test seam: forget the page session's pick. */
export function __resetExploreVideoPick() {
  sessionPick = null
}

/**
 * One clip from *videos*, the unfinished ones the server listed. The first call
 * draws at random; later calls keep that clip while it is still listed, and
 * draw again only once it is finished (gone from the list).
 */
export function pickExploreVideo(videos: FeatureVideo[], random: () => number = Math.random): FeatureVideo | null {
  if (videos.length === 0) return null
  const kept = videos.find(v => v.id === sessionPick)
  if (kept) return kept
  const drawn = videos[Math.min(videos.length - 1, Math.floor(random() * videos.length))]
  sessionPick = drawn.id
  return drawn
}

/**
 * The video card at the top of Explore, the one place feature videos show. It
 * plays one clip drawn at random from the ones this machine has not finished
 * (`GET /api/feature-videos/library`) and is hidden when none are left. Watching
 * or closing records the clip's permanent Feature Videos verdict (RFC 8.2), so
 * it holds on this machine across browsers. Fixed layout; the clip's title is
 * manifest data.
 */
export default function ExploreIntroVideo({ className }: { className?: string }) {
  const qc = useQueryClient()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The slot's key reaches the server's restricted-session gates, as in Settings.
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  // Read fresh each time Explore opens, and every minute while it is open with
  // nothing listed: the background download lands clips without telling the
  // page. The pick survives a refresh (`sessionPick`).
  const libraryQ = useQuery<FeatureVideoLibrary>({
    queryKey: ['featureVideoLibrary', sessionKey],
    queryFn: () => api.featureVideoLibrary(sessionKey),
    refetchOnMount: 'always',
    refetchInterval: q => (q.state.data && q.state.data.enabled && q.state.data.videos.length === 0 ? LIBRARY_POLL_MS : false),
  })
  const [closed, setClosed] = useState(false)
  // The clip being played. Held, so the list refreshing after its `seen` verdict
  // cannot swap the player for another clip mid-play.
  const [held, setHeld] = useState<FeatureVideo | null>(null)
  const [failed, setFailed] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  // `seenSent`: the clip played far enough to count as watched. `saved`: a
  // verdict reached the server. Close saves whatever is not saved yet, so a
  // failed write is retried there, never in a loop from playback events.
  const seenSent = useRef(false)
  const saved = useRef(false)
  if (closed) return null
  // Hidden until the list is read, so a finished card never flashes in. A failed
  // read shows the card with its error rather than hiding it.
  const video = held ?? (libraryQ.data ? pickExploreVideo(libraryQ.data.videos) : null)
  if (!video && !libraryQ.isError) return null
  const playing = held !== null
  const record = (id: string, status: 'seen' | 'dismissed'): Promise<boolean> =>
    api.featureVideoFeedback(id, status, sessionKey).then(
      () => {
        saved.current = true
        setSaveFailed(false)
        // The Settings readout shares the verdict map, so both refresh.
        void qc.invalidateQueries({ queryKey: ['featureVideoLibrary'] })
        void qc.invalidateQueries({ queryKey: ['featureVideoStatus'] })
        return true
      },
      (e: unknown) => {
        recordError({
          source: 'api',
          message: i18nT('milestones.intro_video_save_failed'),
          endpoint: '/api/feature-videos/feedback',
          detail: e instanceof Error ? e.message : String(e),
        })
        setSaveFailed(true)
        return false
      },
    )
  // Watched: record it, but leave the player up so the clip can finish.
  const seen = () => {
    if (!video || seenSent.current) return
    seenSent.current = true
    void record(video.id, 'seen')
  }
  // The card goes only once its verdict is saved; a failed save keeps it up
  // with the error, and the next Close tries again. With no clip (the list
  // failed to load) there is no verdict to save.
  const close = () => {
    if (!video || saved.current) {
      setClosed(true)
      return
    }
    void record(video.id, seenSent.current ? 'seen' : 'dismissed').then(ok => { if (ok) setClosed(true) })
  }
  return (
    <Glass variant="panel" radius={16} className={className} data-testid="explore-intro-video">
      <div className="flex items-center gap-3">
        <span aria-hidden="true" className="shrink-0 w-7 h-7 rounded-md flex items-center justify-center bg-accent-subtle text-accent">
          <Play size={14} />
        </span>
        <span className="flex-1 min-w-0 truncate text-[13px] font-semibold text-text-strong">{video?.title}</span>
        {video && !playing && !failed && (
          <button
            type="button"
            data-testid="explore-intro-video-watch"
            className="shrink-0 text-[13px] font-medium text-accent hover:underline bg-transparent border-none cursor-pointer"
            onClick={() => setHeld(video)}
          >
            {i18nT('milestones.intro_video_watch')}
          </button>
        )}
        <button
          type="button"
          data-testid="explore-intro-video-close"
          aria-label={i18nT('milestones.intro_video_close')}
          title={i18nT('milestones.intro_video_close')}
          className="shrink-0 p-1 rounded-md text-muted hover:text-text bg-transparent border-none cursor-pointer"
          onClick={close}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      {/* The panel holds no draft, so handing a failure to the agent loses nothing. */}
      {libraryQ.isError && !video && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('milestones.video_failed')} variant="inline" askAgent testId="explore-intro-video-status-error" />
        </div>
      )}
      {saveFailed && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('milestones.intro_video_save_failed')} variant="inline" askAgent testId="explore-intro-video-save-error" />
        </div>
      )}
      {failed && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('milestones.video_failed')} variant="inline" askAgent testId="explore-intro-video-error" />
        </div>
      )}
      {video && playing && !failed && (
        <div className="mt-2">
          {/* Feature clips carry no captions track. */}
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            data-testid="explore-intro-video-player"
            className="w-full rounded-lg bg-bg aspect-video"
            src={video.src}
            poster={video.poster}
            aria-label={video.title}
            controls
            playsInline
            autoPlay
            onTimeUpdate={e => {
              const el = e.currentTarget
              if (Number.isFinite(el.duration) && el.duration > 0 && el.currentTime / el.duration >= SEEN_AT) seen()
            }}
            onEnded={seen}
            onError={e => {
              const err = e.currentTarget.error
              recordError({
                source: 'api',
                message: i18nT('milestones.video_failed'),
                endpoint: video.src,
                code: err ? String(err.code) : undefined,
                detail: err?.message || undefined,
              })
              setFailed(true)
            }}
          />
        </div>
      )}
    </Glass>
  )
}
