import { useRef, useState } from 'react'
import { Play, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import Glass from '../Glass'
import ErrorNotice from '../ErrorNotice'
import { recordError } from '../../utils/errorReport'

/** Set once the intro clip was watched or closed: the card never comes back. */
export const INTRO_VIDEO_DONE_KEY = 'mc-explore-intro-video-done'

/** The clip and its poster ship with the dashboard (`public/app-assets/feature-videos/`),
 *  so no read decides whether there is one and nothing is fetched from elsewhere. */
export const INTRO_VIDEO_SRC = '/app-assets/feature-videos/kiro-crew-intro.mp4'
export const INTRO_VIDEO_POSTER = '/app-assets/feature-videos/kiro-crew-intro.en.jpg'

/** Share of the clip after which it counts as watched, as in the startup intro. */
const SEEN_AT = 0.8

/**
 * "Kiro Crew in 15 seconds": a slim card at the top of Explore with one short
 * bundled video. It shows until the clip is watched or the card is closed;
 * after that a local flag keeps it gone for good. Fixed layout, i18n text only.
 */
export default function ExploreIntroVideo({ className }: { className?: string }) {
  const [done] = useState(() => safeGetItem(INTRO_VIDEO_DONE_KEY) === '1')
  const [closed, setClosed] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [failed, setFailed] = useState(false)
  const seenSent = useRef(false)
  if (done || closed) return null
  const title = i18nT('milestones.intro_video_title')
  // Watched: remember it, but leave the player up so the clip can finish.
  const seen = () => {
    if (seenSent.current) return
    seenSent.current = true
    safeSetItem(INTRO_VIDEO_DONE_KEY, '1')
  }
  const close = () => {
    safeSetItem(INTRO_VIDEO_DONE_KEY, '1')
    setClosed(true)
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
      {/* The panel holds no draft, so handing the failure to the agent loses nothing. */}
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
