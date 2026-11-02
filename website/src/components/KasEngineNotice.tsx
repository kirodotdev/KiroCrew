import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type KasBundleStatus } from '../api/client'
import type { AcpBackendConfig } from '../api/acpBackend'
import { ACP_BACKEND_KAS } from '../api/acpBackend'
import { fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { reportForError } from '../utils/errorReport'
import ErrorNotice from './ErrorNotice'
import StatusNotice from './StatusNotice'
import { Btn } from './ui'

/** Same cache entry the Agent Harness engine card reads (`KasBundleCard`). */
const STATUS_QUERY_KEY = ['kas-bundle'] as const

/** Poll cadence while the gateway is downloading the engine. */
export const KAS_ENGINE_INSTALL_POLL_MS = 2_000
/**
 * Poll cadence otherwise, so a download a chat started is noticed. The status
 * read is a config read and a directory stat in the gateway: no network.
 */
export const KAS_ENGINE_IDLE_POLL_MS = 20_000

/**
 * App-level notice for Kiro Crew's own KAS engine, mounted beside the sign-in
 * notice. Choosing KAS runs Crew's own engine where kiro-cli cannot run, and the
 * gateway downloads that engine the first time a chat needs it. The download
 * takes a while, and nothing else on screen says why the chat has not started,
 * so this says so while it runs; a failed download says why and how to retry
 * (the gateway's own text names `kirocrew kas install`), with a Retry button.
 *
 * Nothing while KAS is not the selected backend, with no install running, or
 * once the engine is installed.
 */
export default function KasEngineNotice() {
  const qc = useQueryClient()
  const [dismissedError, setDismissedError] = useState<string | null>(null)
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfgQuery.data?.agent?.acp_backend === ACP_BACKEND_KAS
  const statusQuery = useQuery<KasBundleStatus>({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    enabled: kas,
    retry: false,
    refetchInterval: query => (query.state.data?.installing
      ? KAS_ENGINE_INSTALL_POLL_MS
      : KAS_ENGINE_IDLE_POLL_MS),
  })
  const retryMut = useMutation({
    mutationFn: () => api.kasBundleInstall(),
    onSuccess: data => qc.setQueryData(STATUS_QUERY_KEY, data),
  })

  const status = statusQuery.data
  if (!kas) return null
  // A failed status read, with or without an earlier answer cached: the lines
  // below would be stale, so this says the read failed instead.
  if (statusQuery.isError) {
    // Keyed by the error text, not its time: the poll keeps failing while the
    // gateway is down, and each failure must not bring a dismissed banner back.
    const key = `status:${statusQuery.error instanceof Error ? statusQuery.error.message : String(statusQuery.error)}`
    if (dismissedError === key) return null
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        message={i18nT('pages.developer.kasBundleCard.status_unavailable')}
        report={reportForError(statusQuery.error)}
        askAgent
        onDismiss={() => setDismissedError(key)}
        testId="kas-engine-status-error"
      />
    )
  }
  if (!status) return null

  if (status.installing || retryMut.isPending) {
    const { done, total } = status.progress
    return (
      <StatusNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        tone="info"
        message={total > 0
          ? i18nT('components.kasEngineNotice.downloading_progress', { percent: fmtPercent(done / total) })
          : i18nT('components.kasEngineNotice.downloading')}
        testId="kas-engine-downloading"
      />
    )
  }

  const error = status.installed ? '' : status.error
  if (!error || dismissedError === error) return null
  // A Retry the gateway refused (another user's tab, an offline gateway) says so
  // under the install's own error rather than leaving the button looking dead.
  const retryError = retryMut.isError
    ? i18nT('components.kasEngineNotice.retry_failed', {
        reason: retryMut.error instanceof Error ? retryMut.error.message : String(retryMut.error),
      })
    : ''
  return (
    <ErrorNotice
      className="mx-3 md:mx-6 mt-4 mb-2"
      title={i18nT('components.kasEngineNotice.failed_title')}
      message={retryError ? `${error}\n${retryError}` : error}
      messagePlacement="below"
      actionPlacement="below"
      footer={(
        <div className="mt-1.5">
          <Btn type="button" primary onClick={() => retryMut.mutate()} data-testid="kas-engine-retry">
            {i18nT('components.kasEngineNotice.retry')}
          </Btn>
        </div>
      )}
      askAgent
      onDismiss={() => setDismissedError(error)}
      testId="kas-engine-failed"
    />
  )
}
