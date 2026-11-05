import { useMutation } from '@tanstack/react-query'
import { Check, FileCode2, Loader2 } from 'lucide-react'

import { ApiError } from '../../api/apiError'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/** The row code the backend stamps on a session start refused because several
 *  spec files declare the agent's name and no pin chooses one, or the pin is
 *  broken (`agent_spec_pin_ops.AGENT_SPEC_AMBIGUOUS_CODE`). */
export const AGENT_SPEC_AMBIGUOUS_CODE = 'agent_spec_ambiguous'

/** One file that declares the contested name, as the row meta carries it. */
export interface AgentSpecChoice {
  file: string
  /** Whole seconds since the epoch, or null when the file's time is unknown. */
  mtime: number | null
  package: string | null
  /** The spec's own description, or null. */
  description: string | null
  /** Spec fields whose value differs from another file's. */
  differs: string[]
}

export interface AgentSpecAmbiguity {
  agent: string
  /** The pinned file when the refusal is a broken pin, else null. */
  pin: string | null
  claimants: AgentSpecChoice[]
  /** Files past the backend's row bound, not listed here. */
  omitted: number
}

/** The contested-spec payload of an error row, or undefined for any other row.
 *  Read from `meta` only: the prose is English and may be translated away. */
export function agentSpecAmbiguity(m: Pick<ChatMessage, 'meta'>): AgentSpecAmbiguity | undefined {
  const meta = m.meta as Record<string, unknown> | undefined | null
  if (!meta || meta.code !== AGENT_SPEC_AMBIGUOUS_CODE) return undefined
  const agent = meta.agent
  const raw = meta.claimants
  if (typeof agent !== 'string' || !agent || !Array.isArray(raw)) return undefined
  const claimants: AgentSpecChoice[] = []
  for (const row of raw) {
    if (!row || typeof row !== 'object') continue
    const r = row as Record<string, unknown>
    if (typeof r.file !== 'string' || !r.file) continue
    claimants.push({
      file: r.file,
      mtime: typeof r.mtime === 'number' && Number.isFinite(r.mtime) ? r.mtime : null,
      package: typeof r.package === 'string' && r.package ? r.package : null,
      description: typeof r.description === 'string' && r.description.trim() ? r.description : null,
      differs: Array.isArray(r.differs)
        ? r.differs.filter((f): f is string => typeof f === 'string' && f.length > 0)
        : [],
    })
  }
  if (claimants.length === 0) return undefined
  const omitted = typeof meta.omitted === 'number' && Number.isInteger(meta.omitted) && meta.omitted > 0 ? meta.omitted : 0
  return { agent, pin: typeof meta.pin === 'string' && meta.pin ? meta.pin : null, claimants, omitted }
}

/**
 * The chooser a refused start offers: each file that declares the agent's name,
 * with its package and modification time, and a "Use this" button that pins it
 * (`PUT /api/agent-spec-pins/{agent}`) and then starts the session again through
 * `onRetry`, the row's Resume. With no `onRetry` (a settled row) the pin is still
 * saved and the card says to send the message again.
 *
 * The list is a labelled `<ul>`; each button names its file and agent for a screen
 * reader, and the outcome line is a polite live region.
 */
export function AgentSpecChoiceCard({ ambiguity, onRetry }: {
  ambiguity: AgentSpecAmbiguity
  onRetry?: () => void
}) {
  useLanguageGeneration()
  const { agent, pin, claimants, omitted } = ambiguity
  const choose = useMutation({
    mutationFn: (file: string) => api.setAgentSpecPin(agent, file),
    onSuccess: () => onRetry?.(),
  })
  const chosen = choose.isSuccess ? choose.variables : undefined
  const pending = choose.isPending ? choose.variables : undefined
  const listId = `agent-spec-choices-${agent}`
  // Once a file is saved only its row stays, so the card reads as settled.
  const rows = chosen === undefined ? claimants : claimants.filter(c => c.file === chosen)
  return (
    <div
      className="bg-warn-subtle ring-1 ring-inset forced-colors:border ring-warn/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
      data-testid="error-card"
      data-agent-spec-ambiguous="true"
    >
      <div className="text-warn-text text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }} id={`${listId}-title`}>
        {chosen !== undefined
          ? i18nT(onRetry ? 'pages.chat.errorCard.spec_choice_done_resuming' : 'pages.chat.errorCard.spec_choice_done', { agent, file: chosen })
          : pin
            ? i18nT('pages.chat.errorCard.spec_choice_broken', { agent, file: pin })
            : i18nT('pages.chat.errorCard.spec_choice_title', { agent })}
      </div>
      <ul className="flex flex-col gap-1.5 m-0 p-0 list-none" aria-labelledby={`${listId}-title`} data-testid="agent-spec-choices">
        {rows.map(c => (
          <li key={c.file} className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0 rounded-md bg-bg-elevated ring-1 ring-inset ring-border px-2.5 py-1.5" data-testid="agent-spec-choice">
            <FileCode2 size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
            <div className="flex flex-col min-w-0 flex-1">
              <span className="font-mono text-[12px] leading-5 text-text" style={{ overflowWrap: 'anywhere' }}>{c.file}</span>
              {c.description && (
                <span className="text-[12px] leading-5 text-text" style={{ overflowWrap: 'anywhere' }} data-testid="agent-spec-choice-description">
                  {c.description}
                </span>
              )}
              {c.differs.length > 0 && (
                <span className="text-[12px] leading-5 text-muted" data-testid="agent-spec-choice-differs">
                  {i18nT('pages.chat.errorCard.spec_choice_differs', { fields: c.differs.join(', ') })}
                </span>
              )}
              <span className="text-[12px] leading-5 text-muted">
                {c.package
                  ? i18nT('pages.chat.errorCard.spec_choice_package', { package: c.package })
                  : i18nT('pages.chat.errorCard.spec_choice_package_unknown')}
                {c.mtime !== null && (
                  <>
                    {' · '}
                    {i18nT('pages.chat.errorCard.spec_choice_modified', { date: fmtDateTime(c.mtime * 1000) })}
                  </>
                )}
              </span>
            </div>
            {chosen === c.file ? (
              <Check size={14} className="lucide-inline shrink-0 text-ok" aria-hidden="true" />
            ) : (
              <Btn
                primary
                type="button"
                onClick={() => choose.mutate(c.file)}
                disabled={choose.isPending}
                aria-label={i18nT('pages.chat.errorCard.spec_choice_use_label', { agent, file: c.file })}
                data-testid="agent-spec-choice-use"
              >
                {pending === c.file && <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />}
                {i18nT('pages.chat.errorCard.spec_choice_use')}
              </Btn>
            )}
          </li>
        ))}
      </ul>
      {chosen === undefined && omitted > 0 && (
        <div className="text-[12px] leading-5 text-muted" data-testid="agent-spec-choice-omitted">
          {i18nT('pages.chat.errorCard.spec_choice_more', { count: omitted, agent })}
        </div>
      )}
      <div aria-live="polite" className="sr-only" data-testid="agent-spec-choice-status">
        {chosen !== undefined &&
          i18nT(onRetry ? 'pages.chat.errorCard.spec_choice_done_resuming' : 'pages.chat.errorCard.spec_choice_done', { agent, file: chosen })}
      </div>
      {choose.isError && (
        // No hand-off: the owner gate or the agents directory refused the pin,
        // and the server's sentence already names what to change.
        <ErrorNotice
          variant="inline"
          title={i18nT('pages.chat.errorCard.spec_choice_failed')}
          message={choose.error instanceof ApiError ? choose.error.message : String(choose.error)}
          noHandoffReason="the pin write was refused by the gateway; its message names the fix"
          testId="agent-spec-choice-error"
        />
      )}
    </div>
  )
}
