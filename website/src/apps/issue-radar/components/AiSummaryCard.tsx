// The AI summary card shared by the issue and pull-request detail panes.
//
// Both panes auto-load a server-side summary when an item opens (cache-first on
// the backend, so re-opening is instant) and offer a small refresh to regenerate.
// The presentation is identical — only the subject noun in the empty state
// differs — so it lives here rather than being copied per pane.
import { useEffect, useState } from 'react'
import { useReducedMotion } from 'framer-motion'
import { Sparkles, RefreshCw } from 'lucide-react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import ShimmerLine from './ShimmerLine'
import { relativeTimeOrDate } from '../lib/format'

import { i18nT } from '../../../i18n/t'
import { fmtDateTimeNumeric } from '../../../i18n/format'
/** Fast typewriter reveal for a freshly-generated summary. Returns a growing
 * prefix of `text` plus a `typing` flag. When `enabled` is false (a cached
 * result the user has already seen, or reduced-motion) it returns the full text
 * immediately — the reveal only plays for a brand-new generation. */
function useTypewriter(text: string, enabled: boolean): { shown: string; typing: boolean } {
  const [shown, setShown] = useState(text)
  useEffect(() => {
    if (!enabled || !text) {
      setShown(text)
      return
    }
    setShown('')
    let i = 0
    const total = text.length
    // ~36 frames at ~22ms → the whole summary types in well under a second.
    const step = Math.max(6, Math.ceil(total / 36))
    const id = window.setInterval(() => {
      i = Math.min(total, i + step)
      setShown(text.slice(0, i))
      if (i >= total) window.clearInterval(id)
    }, 22)
    return () => window.clearInterval(id)
  }, [text, enabled])
  return { shown, typing: shown.length < text.length }
}

/** The AI summary card at the top of a detail pane's main column. Auto-loads
 * when the item opens (cache-first server-side); a small refresh regenerates. */
export default function AiSummaryCard({
  summary, fromCache, loading, fetching, error, onRegenerate, subject = 'issue', generatedAt,
  staleSince = null,
}: {
  summary: string
  fromCache: boolean
  loading: boolean
  fetching: boolean
  error: Error | null
  onRegenerate: () => void
  /** Noun used in the empty state ("No summary available for this …"). */
  subject?: string
  /** ISO timestamp the current summary was generated, if known. */
  generatedAt?: string | null
  /** ISO timestamp of activity NEWER than the summary, when there is any.
   *
   * The summary deliberately does not regenerate on its own (that would spend a
   * model call every time a comment lands while the pane sits open), so the age
   * label has to be honest about the gap instead: with this set it reads in a
   * warning tone and says the summary predates the newest activity. */
  staleSince?: string | null
}) {
  const reduce = useReducedMotion()
  // Typewriter only for a freshly-generated summary (not a cached one the user
  // has already seen, and not under reduced-motion).
  const { shown, typing } = useTypewriter(summary, !fromCache && !!summary && !reduce)
  // A regenerate gets the SAME treatment as a first generation: the stale
  // summary is replaced by the shimmer immediately, so it is never left sitting
  // there looking current while a new one is being written.
  const generating = loading || fetching
  // Re-render on a timer so the age label ticks up while the card stays open
  // (a summary can sit on screen for many minutes).
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!generatedAt || generating) return
    const id = window.setInterval(() => setTick((n) => n + 1), 30_000)
    return () => window.clearInterval(id)
  }, [generatedAt, generating])
  const age = !generating && generatedAt ? relativeTimeOrDate(generatedAt) : ''
  const stale = Boolean(age && staleSince)
  return (
    <div className="mb-5 rounded-lg border border-accent/25 bg-accent-subtle/40 overflow-hidden">
      <div className="flex items-center gap-1.5 px-3.5 py-2 border-b border-accent/20 text-[11px] uppercase tracking-wider text-accent font-medium">
        <Sparkles size={12} className={generating || typing ? 'animate-pulse' : ''} /> {i18nT('apps.issueRadar.components.aiSummaryCard.ai_summary')}
        {age && (
          <span
            className={
              'ml-auto normal-case tracking-normal ' +
              (stale ? 'text-warn' : 'text-accent/60')
            }
            title={
              stale
                ? i18nT('apps.issueRadar.components.aiSummaryCard.generated_stale', {
                  generated: fmtDateTimeNumeric(generatedAt as string),
                  since: fmtDateTimeNumeric(staleSince as string),
                })
                : i18nT('apps.issueRadar.components.aiSummaryCard.generated', {
                  generated: fmtDateTimeNumeric(generatedAt as string),
                })
            }
          >
            {age}{stale ? <> · {i18nT('apps.issueRadar.components.aiSummaryCard.outdated')}</> : ''}
          </span>
        )}
        <button
          onClick={onRegenerate}
          disabled={fetching}
          title={i18nT('apps.issueRadar.components.aiSummaryCard.regenerate_summary')}
          aria-label={i18nT('apps.issueRadar.components.aiSummaryCard.regenerate_ai_summary')}
          className={
            'inline-flex items-center text-accent/70 hover:text-accent disabled:opacity-40 ' +
            'cursor-pointer bg-transparent p-0.5' + (age ? '' : ' ml-auto')
          }
        >
          <RefreshCw size={12} className={fetching ? 'animate-spin' : ''} />
        </button>
      </div>
      <div className="px-3.5 py-2.5 text-[13px] leading-relaxed text-text">
        {generating ? (
          <div role="status" aria-label={i18nT('apps.issueRadar.components.aiSummaryCard.generating_ai_summary')}>
            <div className="flex items-center gap-1.5 text-[11.5px] text-accent mb-2">
              <Sparkles size={12} className="animate-pulse" />
              <span className="animate-pulse">{i18nT('apps.issueRadar.components.aiSummaryCard.reading_summarizing')}</span>
            </div>
            <div className="space-y-1.5">
              <ShimmerLine w="100%" />
              <ShimmerLine w="94%" delay={0.12} />
              <ShimmerLine w="72%" delay={0.24} />
            </div>
          </div>
        ) : error && summary ? (
          // A failed REGENERATE keeps the previous summary on screen — throwing it
          // away would lose good content — but must say so, or the stale text plus
          // its old timestamp reads as a successful refresh.
          <>
            <div className="mb-2 text-[11.5px] text-danger">
              {i18nT('apps.issueRadar.components.aiSummaryCard.couldn_t_regenerate_showing_the_previous_summary')}{' '}
              <button onClick={onRegenerate} className="underline cursor-pointer bg-transparent text-danger">{i18nT('apps.issueRadar.components.aiSummaryCard.retry')}</button>
            </div>
            <MarkdownRenderer content={summary} />
          </>
        ) : error ? (
          <span className="text-danger">
            {i18nT('apps.issueRadar.components.aiSummaryCard.couldn_t_generate_a_summary')}{' '}
            <button onClick={onRegenerate} className="underline cursor-pointer bg-transparent text-danger">{i18nT('apps.issueRadar.components.aiSummaryCard.retry')}</button>
          </span>
        ) : summary ? (
          <MarkdownRenderer content={typing ? shown : summary} />
        ) : (
          <span className="text-muted">{i18nT('apps.issueRadar.components.aiSummaryCard.no_summary_available_for_this')} {subject}.</span>
        )}
      </div>
    </div>
  )
}
