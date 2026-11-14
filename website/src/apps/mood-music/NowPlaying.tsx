/**
 * The now-playing card: the player, the song, why it fits, and like / skip.
 *
 * A YouTube pick plays in a sandboxed iframe on the privacy-enhanced embed host
 * (the only one the dashboard CSP frames), its URL built from the checked video
 * id. A bilibili pick is a link that opens a new tab in the person's own
 * browser, re-checked before it is drawn.
 */
import { ExternalLink, Heart, SkipForward, Sparkles } from 'lucide-react'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { QueuedPick } from './api'
import { bilibiliUrl, youtubeEmbedUrl } from './links'

export default function NowPlaying({ pick, autoplay, busy, onLike, onSkip, onMore }: {
  pick: QueuedPick
  autoplay: boolean
  busy: boolean
  onLike: () => void
  onSkip: () => void
  onMore: () => void
}) {
  const embed = pick.source === 'youtube' ? youtubeEmbedUrl(pick.video_id, autoplay) : null
  const link = pick.source === 'bilibili' ? bilibiliUrl(pick.url) : null
  const liked = pick.feedback === 'liked'

  return (
    <section
      aria-label={i18nT('apps.moodMusic.page.now_playing')}
      className="rounded-2xl border border-[var(--border)] bg-[color-mix(in_oklab,var(--card)_82%,transparent)] p-4 shadow-sm backdrop-blur-md"
      data-testid="mood-now-playing"
    >
      <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        {i18nT('apps.moodMusic.page.now_playing')}
      </p>
      {embed && (
        <div className="mt-3 aspect-video w-full overflow-hidden rounded-xl bg-[var(--bg-elevated)]">
          <iframe
            key={embed}
            src={embed}
            title={i18nT('apps.moodMusic.page.player_title', { title: pick.title })}
            className="h-full w-full border-0"
            allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
            allowFullScreen
            referrerPolicy="strict-origin-when-cross-origin"
            sandbox="allow-scripts allow-same-origin allow-presentation allow-popups"
            data-testid="mood-youtube-frame"
          />
        </div>
      )}
      {link && (
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 flex aspect-[3/1] w-full flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-[var(--border)] text-[var(--accent)] no-underline hover:bg-[var(--bg-hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
          data-testid="mood-bilibili-link"
        >
          <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
            <ExternalLink size={15} aria-hidden="true" />
            {i18nT('apps.moodMusic.page.open_bilibili')}
          </span>
          <span className="text-xs text-[var(--muted)]">{i18nT('apps.moodMusic.page.bilibili_note')}</span>
        </a>
      )}
      <h2 className="mt-3 text-lg font-semibold leading-snug text-[var(--text)]">{pick.title}</h2>
      {pick.artist && <p className="text-sm text-[var(--muted)]">{pick.artist}</p>}
      {pick.why && (
        <p className="mt-2 text-sm leading-relaxed text-[var(--text)]">
          <span className="sr-only">{i18nT('apps.moodMusic.page.why')}: </span>
          {pick.why}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <Btn onClick={onLike} disabled={busy} aria-pressed={liked} primary={liked}>
          <Heart size={14} aria-hidden="true" fill={liked ? 'currentColor' : 'none'} />
          {liked ? i18nT('apps.moodMusic.page.liked') : i18nT('apps.moodMusic.page.like')}
        </Btn>
        <Btn onClick={onSkip} disabled={busy}>
          <SkipForward size={14} aria-hidden="true" />
          {i18nT('apps.moodMusic.page.skip')}
        </Btn>
        <Btn onClick={onMore} disabled={busy}>
          <Sparkles size={14} aria-hidden="true" />
          {i18nT('apps.moodMusic.page.more')}
        </Btn>
      </div>
    </section>
  )
}
