/**
 * Leaked agent runtimes: managed runtimes no session tracks, still holding memory.
 *
 * Reads `GET /api/system/leaked-runtimes`, the reconciler's last reading. The card
 * self-hides when the route is absent (404, an older gateway), when the gateway
 * says it cannot read here (a non-Linux host) and when nothing is leaked. Any other
 * read failure renders through ErrorNotice, so a broken reading never looks healthy.
 *
 * Reclaim is the shared arm→Confirm→decay machine (`useArmedDelete`): the first
 * click only arms, the second calls the owner-only route with `confirm: true`,
 * which runs the gateway's gated kill path once. The armed label states what the
 * second click ends and is the accessible name, so it carries no aria-label.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import type { LeakedRuntimes, LeakedRuntimesReclaim } from '../../api/client/system'
import { Btn, Card, CardTitle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { useArmedDelete } from '../../hooks/useArmedDelete'
import { fmtBytes, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

const RECLAIM_ID = 'reclaim'

/** The reclaim route's owner gate answers 403 with `code: 'owner_only'`. */
function isOwnerOnly(e: unknown): boolean {
  const r = e as { status?: unknown; body?: unknown }
  return r?.status === 403 && typeof r.body === 'string' && r.body.includes('owner_only')
}

export default function LeakedRuntimesCard() {
  const queryClient = useQueryClient()
  const { data, error: loadError } = useQuery<LeakedRuntimes>({
    queryKey: ['leakedRuntimes'],
    queryFn: () => api.leakedRuntimes(),
    refetchInterval: 30_000,
    retry: false,
  })
  const reclaim = useMutation<LeakedRuntimesReclaim>({
    mutationFn: () => api.reclaimLeakedRuntimes(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['leakedRuntimes'] }),
  })
  const { armedId, arm, confirm, isDeleting } = useArmedDelete(() => reclaim.mutateAsync())
  const armed = armedId === RECLAIM_ID
  const result = reclaim.data

  const loadFailed = !!loadError && !isNotFoundError(loadError)

  if (loadFailed) {
    return (
      <Card data-testid="leaked-runtimes-card">
        <CardTitle>{i18nT('pages.servicesTab.leaked_runtimes')}</CardTitle>
        {/* No unsaved input lives on this card, so the hand-off cannot lose anything. */}
        <ErrorNotice
          message={i18nT('pages.servicesTab.leaked_load_failed')}
          askAgent
          testId="leaked-runtimes-load-error"
        />
      </Card>
    )
  }
  if (!data?.supported || (data.count === 0 && !result && !reclaim.error)) return null

  return (
    <Card data-testid="leaked-runtimes-card">
      <CardTitle>
        {i18nT('pages.servicesTab.leaked_runtimes')}
        <InfoTip text={i18nT('pages.servicesTab.leaked_runtimes_tip')} />
      </CardTitle>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12.5px] mb-3">
        <dt className="text-muted">{i18nT('pages.servicesTab.leaked_count')}</dt>
        <dd className="text-text-strong" data-testid="leaked-runtimes-count">{fmtNumber(data.count)}</dd>
        <dt className="text-muted">{i18nT('pages.servicesTab.leaked_memory')}</dt>
        <dd className="text-text-strong" data-testid="leaked-runtimes-rss">{fmtBytes(data.rss_bytes)}</dd>
      </dl>
      <p className="text-[12.5px] text-muted mb-3" data-testid="leaked-runtimes-no-loss">
        {i18nT('pages.servicesTab.leaked_no_loss')}
      </p>
      {reclaim.error && (
        // No unsaved input lives on this card, so the hand-off cannot lose anything.
        <ErrorNotice
          message={isOwnerOnly(reclaim.error)
            ? i18nT('pages.servicesTab.leaked_owner_only')
            : reclaim.error.message}
          onDismiss={() => reclaim.reset()}
          askAgent
          className="mb-3"
          testId="leaked-runtimes-error"
        />
      )}
      {result && (
        <p className="text-[12.5px] text-muted mb-3" data-testid="leaked-runtimes-result">
          {i18nT('pages.servicesTab.leaked_reclaim_result', {
            ended: fmtNumber(result.killed.length),
            kept: fmtNumber(result.refused.length),
          })}
        </p>
      )}
      {data.count > 0 && (
        <Btn
          danger={armed}
          disabled={isDeleting(RECLAIM_ID)}
          onClick={() => { if (armed) void confirm(RECLAIM_ID); else arm(RECLAIM_ID) }}
          data-testid="leaked-runtimes-reclaim"
        >
          {armed ? i18nT('pages.servicesTab.leaked_reclaim_confirm') : i18nT('pages.servicesTab.leaked_reclaim')}
        </Btn>
      )}
    </Card>
  )
}
