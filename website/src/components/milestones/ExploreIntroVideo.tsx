import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { api } from '../../api/client'
import type { FeatureVideoStatus } from '../../api/client/featureDiscovery'
import { useAppSelector } from '../../store'
import Glass from '../Glass'
import ErrorNotice from '../ErrorNotice'
import { recordError } from '../../utils/errorReport'

/** The clip's Feature Videos catalog id (`feature_videos.py`, kept out of the
 *  startup popup). Its `seen` / `dismissed` verdict is what retires this card. */
export const INTRO_VIDEO_ID = 'kiro-crew-intro'

/** The clip and its poster ship with the dashboard (`public/app-assets/feature-videos/`). */
export const INTRO_VIDEO_SRC = '/app-assets/feature-videos/kiro-crew-intro.mp4'
export const INTRO_VIDEO_POSTER = '/app-assets/feature-videos/kiro-crew-intro.jpg'

/** Share of the clip after which it counts as watched, as in the startup intro. */
const SEEN_AT = 0.8

/**
 * "Kiro Crew in 15 seconds": a slim card at the top of Explore with one short
 * bundled video. It shows until the clip is watched or the card is closed, and
 * that verdict is the same permanent Feature Videos record the startup intros
 * use (RFC 8.2), so it holds on this machine across browsers. Fixed layout,
 * i18n text only.
 */
export default function ExploreIntroVideo({ className }: { className?: string }) {
  const qc = useQueryClient()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The slot's key reaches the server's restricted-session gates, as in Settings.
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  // Same query key as the Settings readout, so a verdict here refreshes both.
  const statusQ = useQuery<FeatureVideoStatus>({
    queryKey: ['featureVideoStatus', sessionKey],
    queryFn: () => api.featureVideoStatus(sessionKey),
  })
  const [closed, setClosed] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [failed, setFailed] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  // `seenSent`: the clip played far enough to count as watched. `saved`: a
  // verdict reached the server. Close saves whatever is not saved yet, so a
  // failed write is retried there, never in a loop from playback events.
  const seenSent = useRef(false)
  const saved = useRef(false)
  // Hidden until the record is read, so a retired card never flashes in. A
  // failed read shows the card with its error rather than hiding it.
  const done = !statusQ.isError && (!statusQ.data || Boolean(statusQ.data.state?.[INTRO_VIDEO_ID]?.status))
  if ((done && !playing) || closed) return null
  const title = i18nT('milestones.intro_video_title')
  const record = (status: 'seen' | 'dismissed'): Promise<boolean> =>
    api.featureVideoFeedback(INTRO_VIDEO_ID, status, sessionKey).then(
      () => {
        saved.current = true
        setSaveFailed(false)
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
    if (seenSent.current) return
    seenSent.current = true
    void record('seen')
  }
  // The card goes only once its verdict is saved; a failed save keeps it up
  // with the error, and the next Close tries again.
  const close = () => {
    if (saved.current) {
      setClosed(true)
      return
    }
    void record(seenSent.current ? 'seen' : 'dismissed').then(ok => { if (ok) setClosed(true) })
  }
  return (
    <Glass variant="panel" radius={16} className={className} data-testid="explore-intro-video">
      <div className="flex items-center gap-3">
        <span aria-hidden="true" className="shrink-0 w-7 h-7 rounded-md flex items-center justify-center bg-accent-subtle text-accent">
          <Play size={14} />
        </span>
        <span className="flex-1 min-w-0 truncate text-[13px] font-semibold text-text-strong">{title}</span>
        {!playing && !failed && (
          <button
            type="button"
            data-testid="explore-intro-video-watch"
            className="shrink-0 text-[13px] font-medium text-accent hover:underline bg-transparent border-none cursor-pointer"
            onClick={() => setPlaying(true)}
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
      {statusQ.isError && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('components.startupVideoModal.media_failed')} variant="inline" askAgent testId="explore-intro-video-status-error" />
        </div>
      )}
      {saveFailed && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('milestones.intro_video_save_failed')} variant="inline" askAgent testId="explore-intro-video-save-error" />
        </div>
      )}
      {failed && (
        <div className="mt-2">
          <ErrorNotice message={i18nT('components.startupVideoModal.media_failed')} variant="inline" askAgent testId="explore-intro-video-error" />
        </div>
      )}
      {playing && !failed && (
        <div className="mt-2">
          {/* The bundled clip carries no captions track. */}
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            data-testid="explore-intro-video-player"
            className="w-full rounded-lg bg-bg aspect-video"
            src={INTRO_VIDEO_SRC}
            poster={INTRO_VIDEO_POSTER}
            aria-label={title}
            controls
            playsInline
            autoPlay
            onTimeUpdate={e => {
              const el = e.currentTarget
              if (Number.isFinite(el.duration) && el.duration > 0 && el.currentTime / el.duration >= SEEN_AT) seen()
            }}
            onEnded={seen}
            onError={e => {
              // Journaled the way the startup intro journals the same failure.
              const err = e.currentTarget.error
              recordError({
                source: 'api',
                message: i18nT('components.startupVideoModal.media_failed'),
                endpoint: INTRO_VIDEO_SRC,
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
