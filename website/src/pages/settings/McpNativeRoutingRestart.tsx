import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type NativeSettingsConsent, type NativeRoutingStatus } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../../components/ui/dialog'
import { i18nT } from '../../i18n/t'

type Proposal = {
  token: string
  running: boolean
  retry: (consent: NativeSettingsConsent) => Promise<NativeRoutingStatus>
}

/** Consent belongs to the exact unsaved change; never silently replay a mutation. */
export function useNativeRoutingRestart() {
  const qc = useQueryClient()
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [error, setError] = useState(false)
  const [transition, setTransition] = useState<string | null>(null)
  const scheduled = (result: NativeRoutingStatus) => {
    if (result.transition_id) setTransition(result.transition_id)
    setProposal(null)
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
  }
  const apply = useMutation({
    mutationFn: (choice: Proposal) => choice.retry({
      restart_native_sessions: true,
      native_settings_confirmation_token: choice.token,
    }),
    onSuccess: scheduled,
    onError: () => {
      setError(true)
      void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
      void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
    },
  })
  const capture = (failure: unknown, retry: Proposal['retry']): boolean => {
    if (!(failure instanceof ApiError) || failure.status !== 409) return false
    try {
      const body: unknown = JSON.parse(failure.body)
      if (!body || typeof body !== 'object' || !('code' in body) || !('confirmation_token' in body)) return false
      if (body.code !== 'native_settings_restart_required' || typeof body.confirmation_token !== 'string') return false
      const work = 'work' in body && body.work && typeof body.work === 'object' ? body.work : null
      const running = !!work && (('turns' in work && Number(work.turns) > 0)
        || ('background' in work && Number(work.background) > 0))
      setError(false)
      setProposal({ token: body.confirmation_token, running, retry })
      return true
    } catch {
      return false
    }
  }
  const shouldPoll = (status?: NativeRoutingStatus): boolean => transition
    ? !(status?.transition_id === transition && ['active', 'activation_failed'].includes(status.status ?? ''))
    : !!status?.starts_blocked && ['stopping', 'saved'].includes(status.status ?? '')

  return {
    capture,
    shouldPoll,
    recovery: () => {
      setError(false)
      setProposal({ token: '', running: true, retry: () => api.restartGateway() as Promise<NativeRoutingStatus> })
    },
    recoveryPending: apply.isPending,
    recoveryError: error && proposal === null,
    dialog: (
      <Dialog open={proposal !== null} onOpenChange={open => { if (!open && !apply.isPending) setProposal(null) }}>
        <DialogContent>
          <DialogHeader>
            <div className="min-w-0 space-y-1 pr-6">
            <DialogTitle>{i18nT('pages.mcpManagement.nativeRouting.confirm_title')}</DialogTitle>
            <DialogDescription>{i18nT('pages.mcpManagement.nativeRouting.confirm_description')}</DialogDescription>
            </div>
          </DialogHeader>
          <DialogBody>
            {proposal?.token && <p>{i18nT('pages.mcpManagement.nativeRouting.confirm_unsaved')}</p>}
            {proposal?.running && <p>{i18nT('pages.mcpManagement.nativeRouting.confirm_running')}</p>}
            {/* No hand-off: this choice is unsaved and belongs to this dialog. */}
            <ErrorNotice message={error ? i18nT('pages.mcpManagement.nativeRouting.request_failed') : null} />
          </DialogBody>
          <DialogFooter>
            <Btn disabled={apply.isPending} onClick={() => setProposal(null)}>{i18nT('pages.mcpManagement.cancel')}</Btn>
            <Btn primary disabled={apply.isPending || !proposal} onClick={() => { if (proposal) apply.mutate(proposal) }}>
              {i18nT('pages.mcpManagement.nativeRouting.confirm_apply')}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    ),
  }
}

const PHASE_KEYS: Record<string, string> = {
  active: 'pages.mcpManagement.nativeRouting.active',
  saved: 'pages.mcpManagement.nativeRouting.saved',
  restart_required: 'pages.mcpManagement.nativeRouting.restart_required',
  stopping: 'pages.mcpManagement.nativeRouting.stopping',
  activation_failed: 'pages.mcpManagement.nativeRouting.activation_failed',
}
const STEP_KEYS: Record<string, string> = {
  claim: 'pages.mcpManagement.nativeRouting.step_claim',
  activation_interrupted: 'pages.mcpManagement.nativeRouting.step_interrupted',
  stop: 'pages.mcpManagement.nativeRouting.step_stop',
  validate_versions: 'pages.mcpManagement.nativeRouting.step_validate',
  save: 'pages.mcpManagement.nativeRouting.step_save',
  broker_activation: 'pages.mcpManagement.nativeRouting.step_activate',
  read_activation_state: 'pages.mcpManagement.nativeRouting.step_read',
}

export function NativeRoutingState({ status, onRestart, pending, requestError }: {
  status?: NativeRoutingStatus
  onRestart: () => void
  pending: boolean
  requestError: boolean
}) {
  const failed = status?.status === 'activation_failed'
  const step = i18nT(STEP_KEYS[status?.failed_step ?? ''] ?? STEP_KEYS.broker_activation)
  return (
    <div className="space-y-2">
      <p role="status" className="text-[13px] text-[var(--muted)]">
        {status?.status ? i18nT(PHASE_KEYS[status.status] ?? PHASE_KEYS.saved) : null}
      </p>
      <ErrorNotice
        message={failed ? i18nT('pages.mcpManagement.nativeRouting.failed_step', {
          step, saved: i18nT(status.saved === null ? 'pages.mcpManagement.nativeRouting.change_unknown' : status.saved ? 'pages.mcpManagement.nativeRouting.change_saved' : 'pages.mcpManagement.nativeRouting.change_unsaved'),
        })
          : requestError ? i18nT('pages.mcpManagement.nativeRouting.request_failed') : null}
        noHandoffReason="Routing activation needs explicit owner review before retry."
      />
      {failed && status.action === 'review_restart_target' && <p className="text-[13px] text-[var(--muted)]">{i18nT('pages.mcpManagement.nativeRouting.review_restart_target')}</p>}
      {failed && <Btn disabled={pending} onClick={onRestart}>{i18nT('pages.mcpManagement.nativeRouting.retry_restart')}</Btn>}
    </div>
  )
}
