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
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Package, RefreshCw, ShieldAlert } from 'lucide-react'

import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import type { MarketAppPending, MarketPlan } from '../../api/client/capabilityManager'
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
        // Keyed by position: two items may declare the same command line.
        : values.map((v, i) => <code key={`${i}:${v}`} className="font-mono text-[12px] text-text break-all">{v}</code>)}
    </li>
  )
}

/** A row step 2 fills in once the app is checked (what the folder sets up). */
function PendingRow({ label }: { label: string }) {
  return (
    <li className="flex flex-col gap-1">
      <span className="font-semibold text-text">{label}</span>
      <span className="text-muted">{i18nT('components.appstore.marketInstall.in_folder_next_step')}</span>
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
  app: 'components.appstore.marketInstall.kind_app',
}

/**
 * The codes /market/confirm answers only when it refused BEFORE the app was
 * installed: a bad request, governance, a changed plan, a spent or expired
 * preparation. Any other failure (a 5xx, a dropped connection, an install that
 * failed part way) may have left the app installed, so it is never shown as
 * "nothing was installed".
 */
const PRE_INSTALL_REFUSALS = new Set([
  'market_refused',
  'market_confirm_invalid',
  'invalid_plan_digest',
  'invalid_market_ref',
  'capability_unavailable',
])

export function confirmRefusedBeforeInstall(e: unknown): boolean {
  return PRE_INSTALL_REFUSALS.has(apiErrorCode(e))
}

function isAppPending(d: unknown): d is MarketAppPending {
  return !!d && typeof d === 'object' && (d as { needsConfirm?: unknown }).needsConfirm === true
}

/**
 * An app's second step: what the verified, unpacked folder holds, shown before the
 * yes. Nothing is installed until the user clicks Install the app.
 */
function AppConfirmView({ pending, error, refused }: { pending: MarketAppPending; error: string | null; refused: boolean }) {
  const c = pending.confirm
  const errorRef = useRef<HTMLDivElement>(null)
  useEffect(() => { if (error) errorRef.current?.scrollIntoView({ block: 'nearest' }) }, [error])
  // The full commands from the plan (argv), not only the program names the installer lists.
  const commands = c.planPermissions?.commands?.length ? c.planPermissions.commands.map(a => a.join(' ')) : c.permissions.commands
  const K = 'components.appstore.marketInstall'
  return (
    <div className="flex flex-col gap-3.5 text-[13px]" data-testid="market-app-confirm">
      {error && (
        <div ref={errorRef} className="flex flex-col gap-1.5" data-testid="market-app-confirm-error">
          {/* No hand-off: this dialog holds the listing the user typed and the prepared install
              awaiting the yes; Start again or Library is the next step, not an agent. */}
          <ErrorNotice message={error} />
          {/* Only a verified refusal says nothing was installed; Start again in the footer
              re-runs the check. Any other failure may have installed the app. */}
          <p className="text-muted leading-relaxed">
            {i18nT(refused ? `${K}.confirm_failed_intro` : `${K}.confirm_outcome_unknown`)}
          </p>
        </div>
      )}
      {(!error || refused) && <p className="text-text leading-relaxed">{i18nT(`${K}.confirm_intro`)}</p>}
      <div className="flex flex-col gap-0.5 rounded-lg border border-border px-3.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="muted">{kindLabel('app')}</Badge>
          <span className="font-semibold text-text break-all min-w-0">{c.app.ref}@{c.app.appVersion || c.app.version}</span>
        </div>
        <span className="text-muted">{i18nT(`${K}.publisher`, { name: c.app.publisher })}</span>
        <code className="font-mono text-[12px] text-muted break-all">{i18nT(`${K}.sha256`, { value: c.app.sha256Prefix })}</code>
        <span className="text-muted">{i18nT(`${K}.app_trust`)}</span>
      </div>
      {c.components.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="font-semibold text-text">{i18nT(`${K}.components_heading`)}</span>
          <ul className="flex flex-col gap-1">
            {c.components.map(comp => (
              <li key={`${comp.kind}:${comp.name}`} className="flex flex-wrap items-center gap-2">
                <Badge variant="muted">{kindLabel(comp.kind)}</Badge>
                <span className="text-text">{comp.name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-col gap-2 rounded-lg border border-warn/30 bg-warn-subtle px-3.5 py-3">
        <div className="flex items-center gap-2 font-semibold text-warn">
          <ShieldAlert size={14} className="shrink-0" />
          <span>{i18nT(`${K}.in_folder_heading`)}</span>
        </div>
        <ul className="flex flex-col gap-2">
          <PermissionRow label={i18nT(`${K}.in_folder_crons`)} meaning="" values={c.inFolder.crons} />
          <PermissionRow label={i18nT(`${K}.in_folder_mcp`)} meaning={i18nT(`${K}.kind_mcp_meaning`)} values={c.inFolder.mcpServers} />
          <PermissionRow label={i18nT(`${K}.in_folder_agents`)} meaning="" values={c.inFolder.agents} />
          <PermissionRow label={i18nT(`${K}.commands_app`)} meaning={i18nT(`${K}.commands_meaning`)} values={commands} />
          <PermissionRow label={i18nT(`${K}.network`)} meaning={i18nT(`${K}.network_meaning`)} values={c.permissions.network} />
          <PermissionRow label={i18nT(`${K}.files`)} meaning={i18nT(`${K}.files_meaning`)} values={c.permissions.files} />
          <PermissionRow label={i18nT(`${K}.secrets_app`)} meaning={i18nT(`${K}.secrets_meaning`)} values={c.planPermissions?.secrets ?? []} />
        </ul>
        <p className="text-warn leading-relaxed">{i18nT(`${K}.not_enforced`)}</p>
      </div>
      <p className="text-muted leading-relaxed">{i18nT(`${K}.never_runs`)}</p>
    </div>
  )
}

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ? i18nT(KIND_LABEL[kind]) : kind
}

/**
 * The page's notice after a marketplace install. An app lands turned off, so the
 * notice says so and links to Library, the same as the install-from-path flow.
 */
export function marketInstalledNotice(installed: MarketInstalled): { message: string; libraryLink: boolean } {
  if (installed.kinds.includes('app')) {
    const listing = listingName(installed.ref)
    const name = listing.slice(listing.indexOf('/') + 1)
    return { message: i18nT('pages.appsPage.installed_app_find_in_library_and_enable', { name }), libraryLink: true }
  }
  return { message: marketInstalledMessage(installed), libraryLink: false }
}

/** The success notice: the listing and the kinds of item it placed, so a page with no new card still reads as done. */
export function marketInstalledMessage(installed: MarketInstalled): string {
  return i18nT('components.appstore.marketInstall.installed', {
    ref: installed.ref,
    kinds: installed.kinds.map(kindLabel).join(', '),
  })
}

/** The listing the user typed, without any @version: the part a different version is pinned onto. */
function listingName(ref: string): string {
  const at = ref.lastIndexOf('@')
  return at > 0 ? ref.slice(0, at) : ref
}

/** The plan's requested item is an app: Install then only prepares, and a second step follows. */
function isAppPlan(plan: MarketPlan | undefined): boolean {
  return !!plan?.items.some(item => item.reason === 'REQUESTED' && item.kind === 'app')
}

function PlanView({ plan }: { plan: MarketPlan }) {
  const p = plan.permissions
  const app = isAppPlan(plan)
  return (
    <div className="flex flex-col gap-3.5 text-[13px]" data-testid="market-plan">
      <p className="text-text leading-relaxed">
        {i18nT(isAppPlan(plan) ? 'components.appstore.marketInstall.app_plan_intro' : 'components.appstore.marketInstall.plan_intro')}
      </p>
      <ul className="flex flex-col gap-2.5 rounded-lg border border-border px-3.5 py-3">
        {plan.items.map(item => (
          <li key={item.ref} className="flex flex-col gap-0.5" data-testid="market-plan-item">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="muted">{kindLabel(item.kind)}</Badge>
              <span className="font-semibold text-text break-all min-w-0">{item.ref}@{item.semver}</span>
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
            {item.kind === 'mcp' && (
              <span className="text-muted">{i18nT('components.appstore.marketInstall.kind_mcp_meaning')}</span>
            )}
          </li>
        ))}
      </ul>
      <div className="flex flex-col gap-2 rounded-lg border border-warn/30 bg-warn-subtle px-3.5 py-3">
        <div className="flex items-center gap-2 font-semibold text-warn">
          <ShieldAlert size={14} className="shrink-0" />
          <span>{i18nT(app ? 'components.appstore.marketInstall.permissions_heading_app' : 'components.appstore.marketInstall.permissions_heading')}</span>
        </div>
        <ul className="flex flex-col gap-2">
          <PermissionRow label={i18nT(app ? 'components.appstore.marketInstall.commands_app' : 'components.appstore.marketInstall.commands')} meaning={i18nT('components.appstore.marketInstall.commands_meaning')} values={p.commands.map(c => c.join(' '))} />
          <PermissionRow label={i18nT('components.appstore.marketInstall.network')} meaning={i18nT('components.appstore.marketInstall.network_meaning')} values={networkValues(p.network)} />
          <PermissionRow label={i18nT('components.appstore.marketInstall.files')} meaning={i18nT('components.appstore.marketInstall.files_meaning')} values={p.files} />
          <PermissionRow label={i18nT(app ? 'components.appstore.marketInstall.secrets_app' : 'components.appstore.marketInstall.secrets')} meaning={i18nT('components.appstore.marketInstall.secrets_meaning')} values={p.secrets} />
          {app && (
            <>
              <PendingRow label={i18nT('components.appstore.marketInstall.in_folder_crons')} />
              <PendingRow label={i18nT('components.appstore.marketInstall.in_folder_mcp')} />
              <PendingRow label={i18nT('components.appstore.marketInstall.in_folder_agents')} />
            </>
          )}
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
  const queryClient = useQueryClient()
  const plan = useMutation({ mutationFn: (r: string) => api.capabilityMarketPlan(r) })
  const [pending, setPending] = useState<MarketAppPending | null>(null)
  const confirmApp = useMutation({
    mutationFn: (p: MarketAppPending) =>
      api.capabilityMarketConfirm(p.ref, plan.data?.planDigest ?? '', p.token),
    onSuccess: (_d, p) => {
      // The app list (sidebar, Library) is cached: tell every reader a new app landed.
      window.dispatchEvent(new Event('mc:apps-changed'))
      onInstalled({ ref: p.ref, kinds: ['app'], specRewritten: true })
      close()
    },
    onError: (e) => {
      // The install may have finished: let the app list (sidebar, Library) read it again.
      if (!confirmRefusedBeforeInstall(e)) window.dispatchEvent(new Event('mc:apps-changed'))
    },
  })
  const apply = useMutation({
    mutationFn: (p: { ref: string; digest: string }) => api.capabilityMarketApply(p.ref, p.digest),
    onSuccess: (d, p) => {
      // An app is only prepared here: the second step shows what is in it, and
      // only its explicit Install sends the yes.
      if (isAppPending(d)) {
        setPending(d)
        return
      }
      // What the install can have placed: skills, MCP servers and themes. Their
      // catalogs are cached with no expiry, so they are refreshed here or the new
      // items stay invisible until a reload.
      for (const key of [['skills'], ['skills-catalog'], ['mcp-servers'], ['mcp-servers-full'], ['custom-themes-catalog']]) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
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

  const busy = apply.isPending || confirmApp.isPending
  const confirmRefused = confirmApp.isError && confirmRefusedBeforeInstall(confirmApp.error)

  /** After a refused yes: drop the prepared install and check the listing again from step 1. */
  function startAgain() {
    setPending(null)
    apply.reset()
    confirmApp.reset()
    plan.mutate(ref.trim())
  }

  function close() {
    setRef('')
    setPending(null)
    plan.reset()
    apply.reset()
    confirmApp.reset()
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      maxWidth={600}
      guardAccidentalDismiss={!!shown}
      dismissDisabled={busy}
      title={i18nT('components.appstore.marketInstall.title')}
      footer={
        <>
          <Btn onClick={close} disabled={busy}>{i18nT('components.appstore.marketInstall.cancel')}</Btn>
          {/* A refused plan offers no way forward: only Cancel, until the listing is edited.
              A yes whose outcome is unknown offers only Cancel too: Library shows what landed. */}
          {pending && confirmApp.isError && !confirmRefused
            ? null
            : pending && confirmRefused
            ? (
              <Btn primary disabled={busy || plan.isPending} onClick={startAgain}>
                <RefreshCw size={14} /> {i18nT('components.appstore.marketInstall.start_again')}
              </Btn>
            )
            : pending
            ? (
              <Btn primary disabled={busy} onClick={() => confirmApp.mutate(pending)}>
                {confirmApp.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.appstore.marketInstall.installing')}</>
                  : <><ShieldAlert size={14} /> {i18nT('components.appstore.marketInstall.install_app')}</>}
              </Btn>
            )
            : blocked ? null : shown
            ? (
              <Btn primary disabled={apply.isPending} onClick={() => apply.mutate({ ref: ref.trim(), digest: shown.planDigest })}>
                {apply.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> {i18nT(isAppPlan(shown) ? 'components.appstore.marketInstall.downloading_checking' : 'components.appstore.marketInstall.installing')}</>
                  : isAppPlan(shown)
                    ? <><Package size={14} /> {i18nT('components.appstore.marketInstall.review_app')}</>
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
            disabled={busy || !!pending}
            onChange={e => { setRef(e.target.value); plan.reset(); apply.reset() }}
            onKeyDown={e => { if (e.key === 'Enter' && ref.trim() && !shown) plan.mutate(ref.trim()) }}
          />
        </label>
        {/* No hand-off: the user retries from this dialog, which keeps the listing they typed. */}
        <ErrorNotice message={plan.isError ? errorText(plan.error) : null} />
        {/* No hand-off: the listing ref typed in this dialog stays; editing it is the next step. */}
        {shown?.refusal && <ErrorNotice message={shown.refusal.message} />}
        {/* A version hint only for a refused version; a conflict names its own fix. */}
        {shown?.refusal && <p className="text-muted leading-relaxed">{i18nT('components.appstore.marketInstall.refused_hint', { ref: listingName(ref.trim()) })}</p>}
        {/* No hand-off: the listing ref typed in this dialog stays; the conflict names what to remove first. */}
        {shown && shown.conflicts.length > 0 && <ErrorNotice message={shown.conflicts.join('; ')} />}
        {/* A refused plan is not offered, so its item list is not shown as if it were. */}
        {shown && !blocked && !pending && <PlanView plan={shown} />}
        {pending && <AppConfirmView pending={pending} error={confirmApp.isError ? errorText(confirmApp.error) : null} refused={confirmRefused} />}
        {/* No hand-off: a refused install is retried here after reviewing the plan again. */}
        <ErrorNotice message={apply.isError ? errorText(apply.error) : null} />
      </div>
    </Modal>
  )
}
