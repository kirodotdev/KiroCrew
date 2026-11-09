import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import type { InstanceView } from '../../api/client/instances'
import type { LocalGrant } from '../../api/client/connectedComputers'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { Btn, PanelSectionHeader } from '../../components/ui'
import { canShareThisComputer, shareThisComputer, type ShareOutcome, type ShareRefusal } from '../../lib/connectedComputers'

const GRANTS_KEY = ['connected-computers', 'local', 'grants'] as const

type GrantState = 'connected' | 'starting' | 'disconnected' | 'refused' | 'unavailable' | 'elsewhere'

/** The controller's English state (`controller.py` `_status`) as one of the states the row names. */
export function grantState(state: string): GrantState {
  if (state === 'connected') return 'connected'
  if (state.startsWith('disconnected')) return 'disconnected'
  if (state.startsWith('refused')) return 'refused'
  if (state.startsWith('unavailable')) return 'unavailable'
  if (state.startsWith('run by another')) return 'elsewhere'
  return 'starting'
}

/** Failure details must stay visible; waiting and connected states are ordinary status. */
export function grantFailure(state: string): string {
  const kind = grantState(state)
  if (kind !== 'unavailable' && kind !== 'refused' && kind !== 'disconnected') return ''
  const detail = state.indexOf(':')
  return detail >= 0 ? state.slice(detail + 1).trim() : state.startsWith('refused by ') ? state : ''
}

/** Crews this computer can share itself with: the enrollment runs over SSH. */
const sshCrews = (instances: InstanceView[] | undefined) =>
  (instances ?? []).filter(i => i.connection_method === 'ssh' && !!i.ssh_host)

/**
 * "Share this computer" (issue 29949), on THIS computer's own dashboard: which
 * Remote Crews may run a chat's files and commands here, a per-crew stop, and
 * one Off for all of them.
 *
 * Starting a share needs the desktop app: its native dialog, folder picker and
 * per-tool questions decide what is granted, never this page (B02, B13).
 * Stopping needs nothing native and works with the feature switch off, because
 * it only narrows.
 */
export function LocalSharingCard() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [outcome, setOutcome] = useState<{ crew: string; result: ShareOutcome } | null>(null)
  const instances = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
  const grants = useQuery({ queryKey: GRANTS_KEY, queryFn: () => api.connectedComputersLocalGrants(), refetchInterval: 10_000 })
  const refresh = () => qc.invalidateQueries({ queryKey: GRANTS_KEY })
  const share = useMutation({
    mutationFn: (crew: InstanceView) => shareThisComputer(() => api.connectedComputersLocalConsent(crew.id)),
    onMutate: () => setOutcome(null),
    onSuccess: (result, crew) => setOutcome({ crew: crew.name, result }),
    onSettled: refresh,
  })
  const revoke = useMutation({ mutationFn: (g: LocalGrant) => api.connectedComputersLocalRevoke(g.grant_id), onSettled: refresh })
  const off = useMutation({ mutationFn: () => api.connectedComputersLocalOff(), onSettled: refresh })

  const bridge = canShareThisComputer()
  const enabled = grants.data?.enabled === true
  const active = (grants.data?.grants ?? []).filter(g => g.status === 'active')
  const sharedWith = new Set(active.map(g => g.instance_id))
  const crews = sshCrews(instances.data?.instances).filter(c => !sharedWith.has(c.id))
  const busy = share.isPending || revoke.isPending || off.isPending
  const reason = (e: unknown) => (e instanceof Error ? e.message : '')

  const refusalText = (r: ShareRefusal, code: string): string => {
    switch (r) {
      case 'needs_app': return t('connectedComputers.local.needsApp')
      case 'not_local': return t('connectedComputers.local.refusal.notLocal')
      case 'disabled': return t('connectedComputers.local.disabled')
      case 'protection_unavailable': return t('connectedComputers.local.refusal.protectionUnavailable')
      case 'protection_broken': return t('connectedComputers.local.refusal.protectionBroken')
      case 'turned_off': return t('connectedComputers.local.refusal.turnedOff')
      case 'enrollment_failed': return t('connectedComputers.local.refusal.enrollmentFailed')
      case 'unknown_instance': return t('connectedComputers.local.refusal.unknownInstance')
      case 'expired': return t('connectedComputers.local.refusal.expired')
      default: return t('connectedComputers.local.refusal.failed', { code })
    }
  }
  const stateLabel = (s: GrantState): string => {
    switch (s) {
      case 'connected': return t('connectedComputers.local.state.connected')
      case 'disconnected': return t('connectedComputers.local.state.disconnected')
      case 'refused': return t('connectedComputers.local.state.refused')
      case 'unavailable': return t('connectedComputers.local.state.unavailable')
      case 'elsewhere': return t('connectedComputers.local.state.elsewhere')
      default: return t('connectedComputers.local.state.starting')
    }
  }
  const askOff = async () => {
    const yes = await confirm({
      title: t('connectedComputers.local.offConfirm.title'),
      body: t('connectedComputers.local.offConfirm.body'),
      confirmLabel: t('connectedComputers.local.offConfirm.confirm'),
    })
    if (yes) off.mutate()
  }

  const result = outcome?.result
  const actionError = share.isError
    ? t('connectedComputers.local.refusal.failed', { code: reason(share.error) })
    : result && !result.ok && result.refusal !== 'cancelled'
      ? refusalText(result.refusal, result.code)
      : revoke.isError && revoke.variables
        ? t('connectedComputers.local.revokeFailed', { name: revoke.variables.gateway_label, reason: reason(revoke.error) })
        : off.isError ? t('connectedComputers.local.offFailed', { reason: reason(off.error) }) : ''

  return (
    <section className="space-y-2">
      <PanelSectionHeader label={t('connectedComputers.local.title')} />
      {grants.isSuccess && enabled && bridge && (
        <p className="text-[13px] text-muted">{t('connectedComputers.local.intro')}</p>
      )}
      {grants.isError && (
        <ErrorNotice variant="inline" askAgent title={t('connectedComputers.local.loadFailed')} message={reason(grants.error)} />
      )}
      {grants.isSuccess && !enabled && <p className="text-[13px] text-muted">{t('connectedComputers.local.disabled')}</p>}
      {grants.isSuccess && enabled && !bridge && <p className="text-[13px] text-muted">{t('connectedComputers.local.needsApp')}</p>}
      {actionError && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={actionError}
          onDismiss={() => { setOutcome(null); share.reset(); revoke.reset(); off.reset() }}
        />
      )}
      {share.isPending && (
        <p role="status" className="inline-flex items-center gap-1.5 text-[13px] text-muted">
          <Loader2 size={13} className="shrink-0 motion-safe:animate-spin" aria-hidden />
          {t('connectedComputers.local.waiting')}
        </p>
      )}
      {result?.ok === false && result.refusal === 'cancelled' && (
        <p role="status" className="text-[13px] text-muted">{t('connectedComputers.local.cancelled')}</p>
      )}
      {result?.ok === true && outcome && (
        <p role="status" className="text-[13px] text-muted">{t('connectedComputers.local.shared', { name: outcome.crew })}</p>
      )}

      {enabled && bridge && crews.length > 0 && (
        <PanelSectionHeader label={t('connectedComputers.local.crews')} />
      )}
      {enabled && bridge && (
        <ul className="space-y-1.5">
          {crews.map(c => (
            <li key={c.id} className="flex items-center gap-2 text-[13px]">
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              <Btn primary disabled={busy}
                aria-label={t('connectedComputers.local.shareNamed', { name: c.name })}
                onClick={() => share.mutate(c)}>
                {t('connectedComputers.local.share')}
              </Btn>
            </li>
          ))}
        </ul>
      )}
      {enabled && bridge && instances.isSuccess && crews.length === 0 && active.length === 0 && (
        <p className="text-[13px] text-muted">{t('connectedComputers.local.noCrews')}</p>
      )}

      {active.length > 0 && (
        <>
          <PanelSectionHeader label={t('connectedComputers.local.sharedWith')} count={active.length} />
          <ul className="space-y-1.5">
            {active.map(g => (
              <li key={g.grant_id} data-cc-grant={g.grant_id} className="flex items-center gap-2 text-[13px]">
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate">{g.gateway_label || g.instance_id}</span>
                  <span className="truncate text-[12px] text-muted font-mono" title={g.root}>{g.root}</span>
                </span>
                {grantFailure(g.state) ? (
                  <ErrorNotice variant="inline" askAgent
                    title={stateLabel(grantState(g.state))} message={grantFailure(g.state)} />
                ) : (
                  <span className="text-muted" title={g.state}>{stateLabel(grantState(g.state))}</span>
                )}
                <Btn disabled={busy}
                  aria-label={t('connectedComputers.local.revokeNamed', { name: g.gateway_label || g.instance_id })}
                  onClick={() => revoke.mutate(g)}>
                  {t('connectedComputers.local.revoke')}
                </Btn>
              </li>
            ))}
          </ul>
          <div>
            <Btn danger disabled={busy} onClick={() => { void askOff() }}>
              {t('connectedComputers.local.off')}
            </Btn>
          </div>
        </>
      )}
      {confirmDialog}
    </section>
  )
}
