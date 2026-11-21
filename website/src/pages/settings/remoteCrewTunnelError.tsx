/**
 * A tunnel failure the gateway names by `error_code`: a plain sentence first, the
 * gateway's own text (AWS's words, ARNs and account IDs included) folded under it.
 */
import { useMemo, type ReactNode } from 'react'
import ErrorNotice from '../../components/ErrorNotice'
import { findReport, recordError, type ErrorReport } from '../../utils/errorReport'
import { i18nT } from '../../i18n/t'

/** The plain sentence for a failure the gateway names by code, or null for any other. */
export function tunnelErrorSummary(code: string | undefined): string | null {
  if (code === 'ssm_start_session_denied') return i18nT('pages.settings.remoteCrewPanel.ssm_start_session_denied')
  return null
}

/** The journal entry for a failure's raw text, recorded when there is none yet, so a
 *  hand-off from the plain sentence still carries AWS's own words. */
export function tunnelErrorReport(raw: string): ErrorReport {
  return findReport(raw) ?? recordError({ source: 'system', message: raw })
}

/** The gateway's own text for a failure, closed until opened. */
export function RawTunnelError({ text }: { text: string }) {
  return (
    <details className="text-[12px] text-muted" data-testid="remote-crew-raw-error">
      <summary className="cursor-pointer">{i18nT('pages.settings.remoteCrewPanel.show_aws_error')}</summary>
      <pre className="mt-1 mb-0 whitespace-pre-wrap font-mono text-[11.5px]" style={{ overflowWrap: 'anywhere' }}>{text}</pre>
    </details>
  )
}

/**
 * A crew row's "Why" for a failing tunnel. A known code shows its sentence with
 * the raw text folded under it; the hand-off carries the raw text through its
 * journal entry (`tunnelErrorReport`). Any other failure shows the gateway's
 * text as before. `askAgent` is safe: every draft this page holds lives in the
 * store, so the hand-off loses nothing.
 */
export function TunnelErrorWhy({ error, code }: { error: string; code?: string }): ReactNode {
  const summary = tunnelErrorSummary(code)
  const report = useMemo(() => (summary && error ? tunnelErrorReport(error) : undefined), [summary, error])
  // `flex-wrap`: a long error pushes the hand-off onto its own line instead of
  // past the card's edge.
  if (!summary) return <ErrorNotice variant="inline" message={error} askAgent className="flex-wrap" />
  return (
    <div className="min-w-0 space-y-1">
      <ErrorNotice variant="inline" message={summary} report={report} askAgent className="flex-wrap" />
      <RawTunnelError text={error} />
    </div>
  )
}
