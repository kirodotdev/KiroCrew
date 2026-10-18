import { useCallback, useEffect, useRef, useState } from 'react'
import { MessageSquarePlus, Send } from 'lucide-react'
import { SendBtn } from './ui'
import { offlineProps } from '../utils/offline'
import { i18nT } from '../i18n/t'

/** Submit-to-chat bar with an optional "Add overall instruction" affordance. The
 *  free-form note is threaded through as the `extraPrompt` arg only when the
 *  toggle is open, and cleared after submit.
 *
 *  Its own module so the chat side panel (`ArtifactPanel`) and the standalone
 *  artifact page's comments sidebar render the SAME bar, rather than growing a
 *  second one that drifts. */
export function SubmitBar({ count, submitting, onSubmit, bleed = false, connected = true, stacked = false }: {
  count: number; submitting: boolean; onSubmit: (extraPrompt?: string) => void
  /** Bleed to the panel edges (non-fullscreen, inside the negative-margin
   *  content wrapper). Fullscreen uses its own padding, so omit it there. */
  bleed?: boolean
  /** Gateway connection flag — disables Submit while offline. */
  connected?: boolean
  /** Put the label on its own line, above the controls. For narrow hosts (the
   *  340px comments sidebar), where one line cuts the label off before it
   *  names where the comments go. */
  stacked?: boolean
}) {
  const [extraPrompt, setExtraPrompt] = useState('')
  const [showExtraPrompt, setShowExtraPrompt] = useState(false)
  const extraPromptRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { if (showExtraPrompt) extraPromptRef.current?.focus() }, [showExtraPrompt])
  const submit = useCallback(() => {
    onSubmit(showExtraPrompt ? extraPrompt : undefined)
    setExtraPrompt('')
    setShowExtraPrompt(false)
  }, [onSubmit, showExtraPrompt, extraPrompt])
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
    </div>
  )
}
