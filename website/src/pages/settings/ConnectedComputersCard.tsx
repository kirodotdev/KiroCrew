import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { ConnectedComputer } from '../../api/client/connectedComputers'
import { ApiError, apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { Btn, PanelSectionHeader } from '../../components/ui'

const QUERY_KEY = ['connected-computers', 'devices'] as const

/** The state a row shows. `removed` rows are never drawn (B12). */
export function deviceState(d: ConnectedComputer): 'online' | 'offline' | 'revoked' {
  if (d.status === 'revoked') return 'revoked'
  return d.connected ? 'online' : 'offline'
}

/** The feature is off on this gateway: the one refusal that hides the card. Auth failures show. */
export function isFeatureOff(err: unknown): boolean {
  return err instanceof ApiError && err.status === 403 && apiErrorCode(err) === 'connected_computers_disabled'
}

/**
 * The user's computers enrolled on this gateway (issue 29949). Rendered only
 * while the preview flag is on; the backend still refuses on its own, and its
 * feature-off 403 hides the card rather than drawing a dead control.
 */
export function ConnectedComputersCard() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const list = useQuery({ queryKey: QUERY_KEY, queryFn: () => api.connectedComputers(), refetchInterval: 10_000 })
  const done = () => qc.invalidateQueries({ queryKey: QUERY_KEY })
  const revoke = useMutation({ mutationFn: (d: ConnectedComputer) => api.connectedComputerRevoke(d.device_id), onSettled: done })
  const remove = useMutation({ mutationFn: (d: ConnectedComputer) => api.connectedComputerRemove(d.device_id), onSettled: done })

  if (list.error && isFeatureOff(list.error)) return null
  const rows = (list.data?.devices ?? []).filter(d => d.status !== 'removed')
  const busy = revoke.isPending || remove.isPending
  const askRemove = async (d: ConnectedComputer) => {
    const yes = await confirm({
      title: t('connectedComputers.removeConfirm.title', { name: d.label }),
      body: t('connectedComputers.removeConfirm.body'),
      confirmLabel: t('connectedComputers.remove'),
    })
    if (yes) remove.mutate(d)
  }
  const reason = (e: unknown) => (e instanceof Error ? e.message : '')
  const actionError = revoke.isError && revoke.variables
    ? t('connectedComputers.revokeFailed', { name: revoke.variables.label, reason: reason(revoke.error) })
    : remove.isError && remove.variables
      ? t('connectedComputers.removeFailed', { name: remove.variables.label, reason: reason(remove.error) })
      : ''
  const stateLabel = (state: ReturnType<typeof deviceState>) => (state === 'online'
    ? t('connectedComputers.state.online')
    : state === 'revoked' ? t('connectedComputers.state.revoked') : t('connectedComputers.state.offline'))

  return (
    <section className="space-y-2">
      <PanelSectionHeader label={t('connectedComputers.title')} count={list.isSuccess ? rows.length : undefined} />
      {list.isError && (
        <ErrorNotice variant="inline" askAgent title={t('connectedComputers.loadFailed')} message={(list.error as Error)?.message} />
      )}
      {actionError && (
        <ErrorNotice variant="inline" askAgent message={actionError} onDismiss={() => { revoke.reset(); remove.reset() }} />
      )}
      {list.isSuccess && rows.length === 0 && (
        <p className="text-[13px] text-muted">{t('connectedComputers.empty')}</p>
      )}
      <ul className="space-y-1.5">
        {rows.map(d => {
          const state = deviceState(d)
          return (
            <li key={d.device_id} data-cc-device={d.device_id} data-cc-state={state}
              className="flex items-center gap-2 text-[13px]">
              {/* The ID gets its own line so a narrow screen never cuts it: it is the only
                  thing that tells two computers with the same name apart. */}
              <span className="min-w-0 flex-1" title={d.root || undefined}>
                <span className="block truncate">{d.label}</span>
                {d.label_is_shared && (
                  <span className="block break-all text-muted">{t('connectedComputers.deviceId', { id: d.device_id })}</span>
                )}
              </span>
              <span className="text-muted">{stateLabel(state)}</span>
              {state !== 'revoked' && (
                <Btn disabled={busy}
                  aria-label={t('connectedComputers.revokeNamed', { name: d.label })}
                  onClick={() => revoke.mutate(d)}>
                  {t('connectedComputers.revoke')}
                </Btn>
              )}
              <Btn danger disabled={busy}
                aria-label={t('connectedComputers.removeNamed', { name: d.label })}
                onClick={() => { void askRemove(d) }}>
                {t('connectedComputers.remove')}
              </Btn>
            </li>
          )
        })}
      </ul>
      {confirmDialog}
    </section>
  )
}
