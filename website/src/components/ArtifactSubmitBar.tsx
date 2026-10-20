import { useCallback, useEffect, useRef, useState } from 'react'
import { MessageSquarePlus, Send } from 'lucide-react'
import { SendBtn } from './ui'
import ErrorNotice from './ErrorNotice'
import { offlineProps } from '../utils/offline'
import { i18nT } from '../i18n/t'

/** What a host's send answers. `false` means nothing was sent (refused, or
 *  not attempted), and a non-empty string means refused for that server
 *  reason. Anything else (`true`, nothing) means delivered. A throw or a
 *  rejection counts as `false`. */
export type SubmitVerdict = void | boolean | string

/** Submit-to-chat bar with an optional "Add overall instruction" affordance. The
 *  free-form note is threaded through as the `extraPrompt` arg only when the
 *  toggle is open, and cleared after submit.
 *
 *  A refused send answers HERE, beside the button, not in a page-level notice
 *  that a scrolled artifact puts off screen (unless `hostReportsRefusal`). It
 *  also hands back the instruction the user typed, so a retry does not mean
 *  typing it again.
 *
 *  Its own module so the chat side panel (`ArtifactPanel`) and the standalone
 *  artifact page's comments sidebar render the SAME bar, rather than growing a
 *  second one that drifts. */
export function SubmitBar({ count, submitting, onSubmit, bleed = false, connected = true, stacked = false, hostReportsRefusal = false }: {
  count: number; submitting: boolean
  onSubmit: (extraPrompt?: string) => SubmitVerdict | Promise<SubmitVerdict>
  /** Bleed to the panel edges (non-fullscreen, inside the negative-margin
   *  content wrapper). Fullscreen uses its own padding, so omit it there. */
  bleed?: boolean
  /** Gateway connection flag — disables Submit while offline. */
  connected?: boolean
  /** Put the label on its own line, above the controls. For narrow hosts (the
   *  340px comments sidebar), where one line cuts the label off before it
   *  names where the comments go. */
  stacked?: boolean
  /** The host already tells the user about a refused send (the chat page's
   *  own send-failure banner), so the bar only hands the instruction back. A
   *  second notice would disagree with the host's and invite a duplicate send. */
  hostReportsRefusal?: boolean
}) {
  const [extraPrompt, setExtraPrompt] = useState('')
  const [showExtraPrompt, setShowExtraPrompt] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const extraPromptRef = useRef<HTMLTextAreaElement>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { if (showExtraPrompt) extraPromptRef.current?.focus() }, [showExtraPrompt])
  const submit = useCallback(() => {
    const note = showExtraPrompt ? extraPrompt : undefined
    setError(null)
    setExtraPrompt('')
    setShowExtraPrompt(false)
    let verdict: SubmitVerdict | Promise<SubmitVerdict>
    try {
      verdict = onSubmit(note)
    } catch {
      verdict = false
    }
    void Promise.resolve(verdict).catch((): SubmitVerdict => false).then(v => {
      if (!mounted.current || (v !== false && typeof v !== 'string')) return
      // A server reason is framed, never shown bare: on its own ("slot agent
      // mismatch") it says neither what failed nor that the comments are safe.
      if (!hostReportsRefusal) {
        setError(v
          ? i18nT('components.artifactPanel.couldn_t_send_your_comments_are_still_pending_reason', { error: v })
          : i18nT('components.artifactPanel.couldn_t_send_your_comments_are_still_pending'))
      }
      // Hand the instruction back, unless the user has typed a new one since.
      if (note) {
        setExtraPrompt(cur => cur || note)
        setShowExtraPrompt(true)
      }
    })
  }, [onSubmit, showExtraPrompt, extraPrompt, hostReportsRefusal])
  const label = (
    <span className={`text-[13px] text-text ${stacked ? 'block break-words' : 'truncate'}`}>
      {i18nT('components.artifactPanel.comment', { count: count })} {i18nT('components.artifactPanel.to_send_to_this_chat')}
    </span>
  )
  const toggle = (
    <button
      type="button"
      aria-label={i18nT('components.artifactPanel.toggle_additional_instruction')}
      title={i18nT('components.artifactPanel.overall_instruction_hint')}
      aria-pressed={showExtraPrompt}
      onClick={() => setShowExtraPrompt(v => !v)}
      className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] font-medium border cursor-pointer transition-all shrink-0 ${showExtraPrompt ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
    ><MessageSquarePlus className="lucide-inline" /> {i18nT('components.artifactPanel.add_instruction')}</button>
  )
  return (
    <div className={`border-t border-border bg-chrome px-3 py-2 ${bleed ? '-mb-4 -ml-4' : 'rounded-md border-x border-b'}`}>
      {stacked && label}
      <div className={`flex items-center justify-between gap-2 ${stacked ? 'mt-1.5' : ''}`}>
        <div className="flex items-center gap-2 min-w-0">
          {!stacked && label}
          {toggle}
        </div>
        <SendBtn onClick={submit} disabled={submitting || !connected} {...offlineProps(connected, i18nT('utils.offline.submit_comments'), i18nT('components.artifactPanel.submit'))}>
          {i18nT('components.artifactPanel.submit')} <Send className="lucide-inline" />
        </SendBtn>
      </div>
      {showExtraPrompt && (
        <textarea
          ref={extraPromptRef}
          aria-label={i18nT('components.artifactPanel.additional_instruction')}
          placeholder={i18nT('components.artifactPanel.optional_overall_feedback_or_an_extra_instructio')}
          value={extraPrompt}
          onChange={e => setExtraPrompt(e.target.value)}
          rows={2}
          className="mt-2 w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-text text-[13px] font-body outline-hidden resize-none focus-ring leading-[18px]"
        />
      )}
      {/* Under the instruction box, so the toggle and its text stay together.
          No hand-off: the overall-instruction draft handed back into this bar is not saved anywhere. */}
      {error && (
        <ErrorNotice message={error} variant="inline" onDismiss={() => setError(null)} className="mt-1.5" />
      )}
    </div>
  )
}
