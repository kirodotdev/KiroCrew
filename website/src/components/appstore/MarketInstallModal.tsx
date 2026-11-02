/**
 * MarketInstallModal — install a marketplace listing (a skill, an MCP server or a
 * theme, with any bundled components and dependencies) through the edition's own
 * installer.
 *
 * Two steps, never one: the user names a listing and reviews the PLAN, which
 * lists every item that would be placed (kind, version, sha256, publisher) and
 * the union of what they may do (commands, network, files, secrets); only then
 * does Install send the plan's digest back. The backend installs only the plan
 * that digest names, so a version, dependency or permission the user did not see
 * cannot ride along. Installing never runs what it installs.
 */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2, Package, ShieldAlert } from 'lucide-react'

import { api } from '../../api/client'
import type { MarketPlan } from '../../api/client/capabilityManager'
import Modal from '../Modal'
import ErrorNotice from '../ErrorNotice'
import { Badge, Btn, Input } from '../ui'
import { i18nT } from '../../i18n/t'
import { agentSpecNotRewritten } from '../AgentSpecNotRewrittenNotice'

function errorText(e: unknown): string {
  return (e as Error)?.message || i18nT('components.appstore.marketInstall.failed')
}

function PermissionRow({ label, meaning, values }: { label: string; meaning: string; values: string[] }) {
  return (
    <li className="flex flex-col gap-1">
      <span className="font-semibold text-text">{label}</span>
      {values.length > 0 && <span className="text-muted">{meaning}</span>}
      {values.length === 0
        ? <span className="text-muted">{i18nT('components.appstore.marketInstall.none')}</span>
        : values.map(v => <code key={v} className="font-mono text-[12px] text-text break-all">{v}</code>)}
    </li>
  )
}

function networkValues(network: MarketPlan['permissions']['network']): string[] {
  if (network === 'none') return []
  if (network === 'any') return [i18nT('components.appstore.marketInstall.network_any')]
  return network
}

/** What a successful install hands the page: the kinds it placed and whether the agent spec followed. */
export interface MarketInstalled {
  ref: string
  kinds: string[]
  specRewritten: boolean
}

const KIND_LABEL: Record<string, string> = {
  skill: 'components.appstore.marketInstall.kind_skill',
  mcp: 'components.appstore.marketInstall.kind_mcp',
  theme: 'components.appstore.marketInstall.kind_theme',
}

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ? i18nT(KIND_LABEL[kind]) : kind
}

function PlanView({ plan }: { plan: MarketPlan }) {
  const p = plan.permissions
  return (
    <div className="flex flex-col gap-3.5 text-[13px]" data-testid="market-plan">
      <p className="text-text leading-relaxed">
        {i18nT('components.appstore.marketInstall.plan_intro')}
      </p>
      <ul className="flex flex-col gap-2.5 rounded-lg border border-border px-3.5 py-3">
        {plan.items.map(item => (
          <li key={item.ref} className="flex flex-col gap-0.5" data-testid="market-plan-item">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="muted">{kindLabel(item.kind)}</Badge>
              <span className="font-semibold text-text">{item.ref}@{item.semver}</span>
              {item.reason === 'DEPENDENCY' && (
                <span className="text-muted">
                  {i18nT('components.appstore.marketInstall.required_by', { ref: item.requiredBy.join(', ') })}
                </span>
              )}
            </div>
            <span className="text-muted">
              {i18nT('components.appstore.marketInstall.publisher', { name: item.publisher.display })}
            </span>
            <code className="font-mono text-[12px] text-muted break-all">
              {i18nT('components.appstore.marketInstall.sha256', { value: item.sha256 })}
            </code>
            <code className="font-mono text-[12px] text-muted break-all">{item.target}</code>
          </li>
        ))}
      </ul>
      <div className="flex flex-col gap-2 rounded-lg border border-warn/30 bg-warn-subtle px-3.5 py-3">
        <div className="flex items-center gap-2 font-semibold text-warn">
          <ShieldAlert size={14} className="shrink-0" />
          <span>{i18nT('components.appstore.marketInstall.permissions_heading')}</span>
        </div>
        <ul className="flex flex-col gap-2">
          <PermissionRow label={i18nT('components.appstore.marketInstall.commands')} meaning={i18nT('components.appstore.marketInstall.commands_meaning')} values={p.commands.map(c => c.join(' '))} />
          <PermissionRow label={i18nT('components.appstore.marketInstall.network')} meaning={i18nT('components.appstore.marketInstall.network_meaning')} values={networkValues(p.network)} />
          <PermissionRow label={i18nT('components.appstore.marketInstall.files')} meaning={i18nT('components.appstore.marketInstall.files_meaning')} values={p.files} />
          <PermissionRow label={i18nT('components.appstore.marketInstall.secrets')} meaning={i18nT('components.appstore.marketInstall.secrets_meaning')} values={p.secrets} />
        </ul>
      </div>
      <p className="text-muted leading-relaxed">{i18nT('components.appstore.marketInstall.never_runs')}</p>
    </div>
  )
}

/**
 * The Sources-popover section that opens the dialog. Renders nothing when this
 * build answers that it installs no marketplace listings, so no control is
 * offered that can only fail; a failed availability check is shown as an error,
 * never silently treated as "unsupported".
 */
export function MarketInstallEntry({ onOpen }: { onOpen: () => void }) {
  const status = useQuery({
    queryKey: ['capability', 'market', 'status'],
    queryFn: () => api.capabilityMarketStatus(),
    staleTime: 5 * 60_000,
    retry: false,
  })
  if (status.isError) {
    // No hand-off: the Sources popover holds the user's typed install path; reopening it retries the check.
    return <ErrorNotice message={errorText(status.error)} />
  }
  if (!status.data?.available) return null
  return (
    <div className="border-t border-border mt-4 pt-4" data-testid="market-install-entry">
      <div className="text-sm font-semibold tracking-tight text-text-strong mb-2">
        {i18nT('components.appstore.marketInstall.section')}
      </div>
      <Btn onClick={onOpen}>
        <Package size={14} /> {i18nT('components.appstore.marketInstall.open')}
      </Btn>
    </div>
  )
}

export default function MarketInstallModal({ open, onClose, onInstalled }: {
  open: boolean
  onClose: () => void
  onInstalled: (installed: MarketInstalled) => void
}) {
  const [ref, setRef] = useState('')
  const plan = useMutation({ mutationFn: (r: string) => api.capabilityMarketPlan(r) })
  const apply = useMutation({
    mutationFn: (p: { ref: string; digest: string }) => api.capabilityMarketApply(p.ref, p.digest),
    onSuccess: (d, p) => {
      onInstalled({
        ref: p.ref,
        kinds: [...new Set((plan.data?.items ?? []).map(item => item.kind))],
        specRewritten: !agentSpecNotRewritten(d),
      })
      close()
    },
  })
  const shown = plan.data
  const blocked = shown && (!shown.installable || shown.conflicts.length > 0)

  function close() {
    setRef('')
    plan.reset()
    apply.reset()
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      maxWidth={600}
      guardAccidentalDismiss={!!shown}
      dismissDisabled={apply.isPending}
      title={i18nT('components.appstore.marketInstall.title')}
      footer={
        <>
          <Btn onClick={close} disabled={apply.isPending}>{i18nT('components.appstore.marketInstall.cancel')}</Btn>
          {/* A refused plan offers no way forward: only Cancel, until the listing is edited. */}
          {blocked ? null : shown
            ? (
              <Btn primary disabled={apply.isPending} onClick={() => apply.mutate({ ref: ref.trim(), digest: shown.planDigest })}>
                {apply.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.appstore.marketInstall.installing')}</>
                  : <><ShieldAlert size={14} /> {i18nT('components.appstore.marketInstall.install')}</>}
              </Btn>
            )
            : (
              <Btn primary disabled={!ref.trim() || plan.isPending} onClick={() => plan.mutate(ref.trim())}>
                {plan.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.appstore.marketInstall.reviewing')}</>
                  : <><Package size={14} /> {i18nT('components.appstore.marketInstall.review')}</>}
              </Btn>
            )}
        </>
      }
    >
      <div className="flex flex-col gap-3.5 text-[13px]">
        <label className="flex flex-col gap-1.5">
          <span className="text-muted">{i18nT('components.appstore.marketInstall.ref_label')}</span>
          <Input
            value={ref}
            disabled={apply.isPending}
            onChange={e => { setRef(e.target.value); plan.reset(); apply.reset() }}
            onKeyDown={e => { if (e.key === 'Enter' && ref.trim() && !shown) plan.mutate(ref.trim()) }}
          />
        </label>
        {/* No hand-off: the user retries from this dialog, which keeps the listing they typed. */}
        <ErrorNotice message={plan.isError ? errorText(plan.error) : null} />
        {/* No hand-off: the listing ref typed in this dialog stays; editing it is the next step. */}
        {shown?.refusal && <ErrorNotice message={shown.refusal.message} />}
        {blocked && <p className="text-muted leading-relaxed">{i18nT('components.appstore.marketInstall.refused_hint')}</p>}
        {/* No hand-off: the listing ref typed in this dialog stays; the conflict names what to remove first. */}
        {shown && shown.conflicts.length > 0 && <ErrorNotice message={shown.conflicts.join('; ')} />}
        {/* A refused plan is not offered, so its item list is not shown as if it were. */}
        {shown && !blocked && <PlanView plan={shown} />}
        {/* No hand-off: a refused install is retried here after reviewing the plan again. */}
        <ErrorNotice message={apply.isError ? errorText(apply.error) : null} />
      </div>
    </Modal>
  )
}
