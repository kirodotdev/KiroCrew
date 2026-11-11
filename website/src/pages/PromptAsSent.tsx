import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'

/** The selected turn's "Prompt as sent": the text that turn sent the agent, secrets
 *  masked. Closed by default; the text is read only while open, so a closed row
 *  costs no request. */
export function PromptAsSent({ slot, turn }: { slot: string; turn: number }) {
  const [open, setOpen] = useState(false)
  // The same disclosure glyph as the block list above it.
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="mt-3 border-t border-border pt-2" data-testid="prompt-as-sent">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 bg-transparent border-0 p-0 appearance-none cursor-pointer text-[13px] text-text hover:text-text-strong rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
      >
        <Chevron size={14} className="shrink-0 text-muted" aria-hidden="true" />
        {i18nT('pages.contextBreakdown.prompt_heading')}
      </button>
      {open ? <PromptText slot={slot} turn={turn} /> : null}
    </div>
  )
}

function PromptText({ slot, turn }: { slot: string; turn: number }) {
  // Read on every open, never polled: a turn's text changes only when it is sent again.
  const { data, error, isLoading } = useQuery({
    queryKey: ['prompt-as-sent', slot, turn],
    queryFn: () => api.telemetryPromptAsSent(slot, turn),
    refetchOnMount: 'always',
    retry: false,
  })
  if (isLoading) {
    return <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.contextBreakdown.prompt_loading')}</p>
  }
  const code = apiErrorCode(error)
  const unavailable =
    code === 'not_kept'
      ? i18nT('pages.contextBreakdown.prompt_not_kept')
      : code === 'too_long'
        ? i18nT('pages.contextBreakdown.prompt_too_long')
        : code === 'owner_only'
          ? i18nT('pages.contextBreakdown.prompt_owner_only')
          : null
  if (unavailable !== null) {
    return (
      <p className="m-0 mt-1 text-[12px] text-muted" data-testid="prompt-as-sent-unavailable">
        {unavailable}
      </p>
    )
  }
  if (error) {
    return <ErrorNotice message={error instanceof Error ? error.message : String(error)} askAgent className="mt-2" />
  }
  return (
    <>
      <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.contextBreakdown.prompt_masked')}</p>
      {/* Focusable so a keyboard can scroll it; never machine-translated, as sent. */}
      <pre
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact prompt.
        tabIndex={0}
        role="region"
        translate="no"
        aria-label={i18nT('pages.contextBreakdown.prompt_heading')}
        className="m-0 mt-1 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg-elevated p-2 font-mono text-[12px] text-text"
        data-testid="prompt-as-sent-text"
      >
        {data?.text ?? ''}
      </pre>
    </>
  )
}
