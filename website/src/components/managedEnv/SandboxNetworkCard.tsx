/**
 * Managed-environment lane: the network-setting step.
 *
 * The setting covers ALL of the user's managed environments, so it is never changed silently:
 *
 *   - `SandboxNetworkAsk`: the confirm dialog shown while the setting is INTEGRATIONS_ONLY. Two
 *     real choices and a cancel; nothing is preselected, there is no remembered "always".
 *   - `SandboxNetworkCard`: the standing line and the "Restore network setting" action, shown
 *     while a saved previous setting exists.
 *
 * Backend: `GET /api/managed-environments/network-step/status`, `POST .../apply`,
 * `POST .../restore` (kiro_crew/managed_env/netcfg_step.py).
 */
import { Btn, Card } from '../ui'
import ErrorNotice from '../ErrorNotice'
import type { NetcfgStepResult, NetcfgStepStatus } from '../../api/managedEnvApi'
import { NETCFG_TEXT } from './text'

export const SANDBOX_NETCFG_QUERY_KEY = ['managed-env-netcfg-step'] as const

/** True when Create must ask first: the setting is read and is the restricted default. */
export function sandboxNetcfgNeedsAsk(s: NetcfgStepStatus | undefined): boolean {
  return Boolean(s && s.code === 'status' && s.needsAgreement)
}

/** What the person was shown, for the server to compare with a fresh read. */
export function sandboxNetcfgSeen(s: NetcfgStepStatus): { networkAccess: string; customDomains: string[] } {
  return { networkAccess: s.networkAccess ?? '', customDomains: s.customDomains ?? [] }
}

export function SandboxNetworkAsk({
  status,
  busy,
  onAgree,
  onWithout,
  onCancel,
}: {
  status: NetcfgStepStatus
  busy: boolean
  onAgree: () => void
  onWithout: () => void
  onCancel: () => void
}) {
  return (
    <div data-testid="sandbox-netcfg-ask" role="alertdialog" aria-label={NETCFG_TEXT.label}>
      <Card className="space-y-3 p-4">
        <h3 className="text-[15px] font-semibold text-text-strong">{NETCFG_TEXT.askTitle}</h3>
        <p className="text-[13px]" data-testid="sandbox-netcfg-dialog-text">
          {status.dialog || NETCFG_TEXT.fallbackDialog}
        </p>
        <p className="text-[12px] text-muted">{status.appliesNote || NETCFG_TEXT.appliesLine}</p>
        <div className="flex flex-wrap gap-2">
          <Btn onClick={onAgree} disabled={busy} data-testid="sandbox-netcfg-agree">
            {NETCFG_TEXT.agree}
          </Btn>
          <Btn onClick={onWithout} disabled={busy} data-testid="sandbox-netcfg-without">
            {NETCFG_TEXT.without}
          </Btn>
          <Btn onClick={onCancel} disabled={busy} data-testid="sandbox-netcfg-cancel">
            {NETCFG_TEXT.cancel}
          </Btn>
        </div>
        <p className="text-[12px] text-muted">{NETCFG_TEXT.withoutWarning}</p>
      </Card>
    </div>
  )
}

export default function SandboxNetworkCard({
  status,
  result,
  busy,
  onRestore,
}: {
  status: NetcfgStepStatus | undefined
  result: NetcfgStepResult | null
  busy: boolean
  onRestore: () => void
}) {
  if (!status && !result) return null
  const canRestore = Boolean(status?.canRestore)
  return (
    <div className="space-y-2" data-testid="sandbox-netcfg-card">
      <p className="text-[12px] text-muted" data-testid="sandbox-netcfg-applies">
        {NETCFG_TEXT.appliesLine}
      </p>
      {canRestore ? (
        <Btn onClick={onRestore} disabled={busy} data-testid="sandbox-netcfg-restore">
          {NETCFG_TEXT.restore}
        </Btn>
      ) : null}
      {result ? (
        result.ok ? (
          <p className="text-[12px]" data-testid="sandbox-netcfg-result">
            {result.message}
          </p>
        ) : (
          <div data-testid="sandbox-netcfg-result">
            <ErrorNotice message={result.message} />
            {result.shown ? (
              <pre className="mt-1 overflow-x-auto text-[11px] text-muted">{JSON.stringify(result.shown, null, 2)}</pre>
            ) : null}
          </div>
        )
      ) : null}
    </div>
  )
}
