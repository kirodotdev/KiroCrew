import { memo, useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'
import { reportForError, type ErrorReport } from '../../utils/errorReport'
import NoticeCard from './NoticeCard'
import { projectHookCommands, type ProjectHooksMeta } from './projectHooksNoticeMatch'

/** One plain sentence per refusal of `PUT /api/project-hooks/consent`, by its code. */
function refusalSentence(err: unknown): string {
  switch (apiErrorCode(err)) {
    case 'dashboard_owner_required':
      return i18nT('pages.chat.projectHooksNotice.err_owner')
    case 'project_hooks_consent_store_full':
      return i18nT('pages.chat.projectHooksNotice.err_full')
    case 'project_hooks_consent_write_failed':
      return i18nT('pages.chat.projectHooksNotice.err_write')
    case 'project_hooks_consent_invalid_body':
      return i18nT('pages.chat.projectHooksNotice.err_folder')
    default:
      return i18nT('pages.chat.projectHooksNotice.err_other')
  }
}

/**
 * The notice with its Turn on / Turn off link and a read-only "Show commands"
 * list, so the owner can read each hook's event and command before choosing. The
 * click records the owner's choice for the folder in Crew; it applies from the
 * next message. Lazy-loaded by the renderer table (`app-sdk/messageRenderers.tsx`).
 */
export default memo(function ProjectHooksNotice({
  message,
  askAgent,
}: {
  message: ChatMessage
  /** Whether the failure notice may offer the agent hand-off, as on SpecHooksNotice. */
  askAgent: boolean
}) {
  useLanguageGeneration()
  const meta = message.meta as ProjectHooksMeta
  const folder = String(meta.project_hooks_folder)
  const startedOn = meta.project_hooks !== 'off'
  const commands = projectHookCommands(meta)
  const savedText = typeof meta.project_hooks_saved_text === 'string' ? meta.project_hooks_saved_text : ''
  // Whether the hooks are on now. Starts from the row, then follows the gateway's
  // own answer (read when the card opens and after each save), so a reloaded row
  // never shows the state it was written in; the opposite link always undoes it.
  const [isOn, setIsOn] = useState(startedOn)
  const enable = !isOn
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'failed'>('idle')
  const [error, setError] = useState<{
    sentence: string
    raw: string
    report?: ErrorReport
  } | null>(null)
  // Each read and save takes a new number; a read answers only if no later read or
  // save started after it, so an older answer never undoes a newer change.
  const seq = useRef(0)
  const [readError, setReadError] = useState<{ report?: ErrorReport } | null>(null)
  const readBack = useCallback(async () => {
    const mine = ++seq.current
    try {
      const now = await api.getProjectHooksConsent(folder)
      if (mine !== seq.current) return
      setIsOn(now.enabled === true)
      setReadError(null)
    } catch (err) {
      if (mine !== seq.current) return
      // Unread: the card keeps what it last knew, and says it could not check.
      setReadError({ report: reportForError(err) })
    }
  }, [folder])
  useEffect(() => {
    void readBack()
  }, [readBack])
  const onClick = async () => {
    seq.current += 1
    setState('saving')
    setError(null)
    try {
      await api.setProjectHooksConsent(folder, enable)
      setIsOn(enable)
      setState('saved')
      void readBack()
    } catch (err) {
      // A plain sentence for the refusal; the gateway's own text goes under Details,
      // and the journaled report rides along so "Ask the agent" hands it over whole.
      setError({
        sentence: refusalSentence(err),
        raw: err instanceof Error && err.message ? err.message : String(err),
        report: reportForError(err),
      })
      setState('failed')
    }
  }
  return (
    <>
      <NoticeCard
        // The card says the state it is in now: the gateway's own text for the
        // other state once a save flipped it, its original text otherwise.
        content={isOn !== startedOn && savedText ? savedText : message.content}
        action={
          <span className="flex items-center gap-2">
            <button
              type="button"
              data-testid="project-hooks-notice-toggle"
              disabled={state === 'saving'}
              className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text"
              onClick={onClick}
            >
              {i18nT(enable ? 'pages.webhooksPage.turn_on' : 'pages.webhooksPage.turn_off')}
            </button>
            {state === 'saved' && (
              <span data-testid="project-hooks-notice-saved" role="status" className="text-muted">
                {i18nT('pages.overview.agentCfgTab.saved')}
              </span>
            )}
          </span>
        }
      />
      {commands.length > 0 && (
        <details className="mt-1 text-[12px] text-muted" data-testid="project-hooks-notice-commands">
          <summary className="cursor-pointer underline underline-offset-2 hover:text-text">
            {i18nT('pages.chat.projectHooksNotice.show_commands')}
          </summary>
          <ul className="mt-1 space-y-1">
            {commands.map((h, i) => (
              <li key={i} className="min-w-0">
                <span className="font-medium">{h.when}</span>{' '}
                <code className="break-all whitespace-pre-wrap">{h.command}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
      <ErrorNotice
        variant="inline"
        title={i18nT('pages.chat.projectHooksNotice.save_failed')}
        message={state === 'failed' && error ? error.sentence : null}
        report={state === 'failed' && error ? error.report : undefined}
        askAgent={askAgent}
        testId="project-hooks-notice-error"
        className="mt-1"
      />
      <ErrorNotice
        variant="inline"
        message={readError && state !== 'failed' ? i18nT('pages.chat.projectHooksNotice.read_failed') : null}
        report={readError?.report}
        askAgent={askAgent}
        testId="project-hooks-notice-read-error"
        className="mt-1"
      />
      {state === 'failed' && error && (
        <details className="mt-1 text-[12px] text-muted" data-testid="project-hooks-notice-error-details">
          <summary className="cursor-pointer">{i18nT('pages.chat.projectHooksNotice.details')}</summary>
          <code className="break-all whitespace-pre-wrap">{error.raw}</code>
        </details>
      )}
    </>
  )
})
