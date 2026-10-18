/**
 * One worktree row's parts: its action buttons, the Pull+Build and provision
 * steppers, the running pod's readout, the provision log panel and the
 * expanded detail panel. The facade's `renderRow` lays them out.
 */
import {
  useState, useRef, useEffect, type CSSProperties, type ReactNode, type Dispatch, type SetStateAction,
} from 'react'
import { Btn, Badge } from '../components/ui'
import Clickable from '../components/Clickable'
import ErrorNotice from '../components/ErrorNotice'
import {
  RefreshCw, Play, Square, ExternalLink, Trash2, LoaderCircle, Check, Video, X, RotateCw, FileText, GitCommit,
  Rocket,
} from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtBytes, fmtPercent } from '../i18n/format'
import {
  filterStepMarkers, syncFailureTail, lastLine, provPhase, fmtElapsed, memColor, type PodResources,
  type IssueRef, type TicketRef, type Worktree, type UndoTarget, type FleetData, type SyncRun, type ProvRun,
  type RebaseResult,
} from './devFleetPageModel'
import { MenuBtn, ConfirmBtn } from './DevFleetPageMenus'

// Auto-scrolling <pre> for the FULL provision log (mirrors the sync log panel's
// styling). Sticks to the bottom while output is still streaming.
export function ProvLogPre({ lines, streaming }: { lines: string[]; streaming: boolean }) {
  const ref = useRef<HTMLPreElement | null>(null)
  useEffect(() => {
    if (streaming && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines, streaming])
  return (
    <pre ref={ref} style={{ margin: '2px 0 8px 32px', padding: '8px 10px', maxHeight: 180, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all', minWidth: 640 } as CSSProperties}>{filterStepMarkers(lines).join('\n') || '(no output yet)'}</pre>
  )
}

export function iconLabel(icon: ReactNode, label: string) {
  return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 } as CSSProperties}>{icon}{label}</span>
}

// Compact inline readout for a running pod: memory against its ceiling, CPU%,
// task count. Each field is rendered ONLY when present — an absent field
// (probe failed, off Linux, accounting off, or first CPU sample) contributes
// nothing, so a blank never reads as a measured 0. Returns null when there is
// nothing at all to show.
export function PodReadout({ r }: { r?: PodResources | null }) {
  if (!r) return null
  const parts: ReactNode[] = []
  const chip: CSSProperties = { fontVariantNumeric: 'tabular-nums', fontFamily: 'ui-monospace, SF Mono, Menlo, monospace' }
  if (r.mem_current != null) {
    const label = r.mem_max != null
      ? fmtBytes(r.mem_current) + ' / ' + fmtBytes(r.mem_max)
      : fmtBytes(r.mem_current)
    parts.push(
      <span key="mem" style={{ ...chip, color: memColor(r.mem_current, r.mem_max) }}
        title={i18nT('pages.devFleetPage.pod_memory_of_ceiling')}>{label}</span>,
    )
  }
  if (r.cpu_pct != null) {
    parts.push(<span key="cpu" style={chip} title={i18nT('pages.devFleetPage.pod_cpu_usage')}>{fmtPercent(r.cpu_pct / 100, { maximumFractionDigits: 1 })}</span>)
  }
  if (r.tasks != null) {
    parts.push(<span key="tasks" style={chip} title={i18nT('pages.devFleetPage.pod_task_count')}>{r.tasks} {i18nT('pages.devFleetPage.pod_tasks_label')}</span>)
  }
  if (parts.length === 0) return null
  return (
    // The readout must never squeeze the worktree NAME out of the row: `flexShrink: 0`
    // made it demand its full intrinsic width, so at a narrow viewport the metrics
    // ran past the cell and the name lost its space. It now shrinks and clips
    // instead, capped so the name always keeps the larger share. The chips are
    // ordered memory -> CPU -> tasks, so what disappears first when space runs out
    // is the least decision-critical figure; memory, the OOM signal, is kept.
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 11, color: 'var(--muted)', flexShrink: 1, minWidth: 0, maxWidth: 'min(340px, 45%)', overflow: 'hidden', whiteSpace: 'nowrap' } as CSSProperties}>
      {parts.map((p, i) => (
        <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          {i > 0 ? <span style={{ opacity: 0.4 }}>{'\u00b7'}</span> : null}{p}
        </span>
      ))}
    </span>
  )
}

/* ─── Detail Panel (expanded row) ─── */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function DetailPanel({ w, d, busy, onRemove, onLoadLogs, logs, logsLoading }: { w: Worktree; d: any; busy: Record<string, boolean>; onRemove: () => void; onLoadLogs: () => void; logs?: string; logsLoading?: boolean }) {
  const mono: CSSProperties = { fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 11.5 }
  const mutedSm: CSSProperties = { fontSize: 11, color: 'var(--muted)', lineHeight: 1.6 }
  const [logsOpen, setLogsOpen] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={mutedSm}>{i18nT('pages.devFleetPage.branch')} <span style={{ ...mono, color: 'var(--text)' }}>{d.branch || '?'}</span></div>
      {d.pr ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.pr')} <a href={d.pr.url || '#'} target="_blank" rel="noopener noreferrer" title={d.pr.title || undefined} style={{ color: 'var(--accent)' }}>
            #{d.pr.number || '?'}{d.pr.title ? ' \u2014 ' + d.pr.title : ''}
          </a>{' '}
          <Badge variant={d.pr.state === 'MERGED' ? 'aim' : d.pr.state === 'OPEN' ? 'ok' : 'warn'}>
            {(d.pr.state || '').toLowerCase()}
          </Badge>
        </div>
      ) : null}
      {d.summary ? (
        <div style={mutedSm}>{i18nT('pages.devFleetPage.purpose')} <span style={{ color: 'var(--text)' }}>{d.summary}</span></div>
      ) : null}
      {d.issues?.length > 0 ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.issues')}{' '}
          {d.issues.map((it: IssueRef, i: number) => (
            it.url
              ? <a key={i} href={it.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>#{it.number}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>#{it.number}</span>
          ))}
        </div>
      ) : null}
      {d.tickets?.length > 0 ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.tickets')}{' '}
          {d.tickets.map((t: TicketRef, i: number) => (
            t.url
              ? <a key={i} href={t.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>{t.id}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>{t.id}</span>
          ))}
        </div>
      ) : null}
      {d.design_docs?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><FileText size={11} className="lucide-inline" /> {i18nT('pages.devFleetPage.design_docs')}</span>
          <ul style={{ margin: '2px 0 0 16px', padding: 0, listStyle: 'none' }}>
            {d.design_docs.map((doc: string, i: number) => <li key={i} style={mono}>{doc}</li>)}
          </ul>
        </div>
      ) : null}
      {d.commits?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><GitCommit size={11} className="lucide-inline" /> {i18nT('pages.devFleetPage.commits')}</span>
          <ul style={{ margin: '2px 0 0 16px', padding: 0, listStyle: 'none' }}>
            {d.commits.map((c: { hash: string; subject: string; when: string }, i: number) => (
              <li key={i} style={{ ...mono, display: 'flex', gap: 8 }}>
                <span style={{ color: 'var(--accent)', flexShrink: 0 }}>{c.hash}</span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.subject}</span>
                <span style={{ color: 'var(--muted)', flexShrink: 0 }}>{c.when}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {d.disk_mb != null ? <div style={mutedSm}>{i18nT('pages.devFleetPage.disk')} {d.disk_mb} {i18nT('pages.devFleetPage.mb')}</div> : null}
      {d.pod_running ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.pod_running_on')}{d.pod_port || '?'}
        </div>
      ) : null}
      {/* Full per-pod resource breakdown for a running pod. Each line renders
          only when its field is present — an absent field (probe failed, off
          Linux, accounting off) shows nothing rather than a measured-looking 0. */}
      {w.pod_resources ? (
        <div style={{ ...mutedSm, display: 'flex', flexDirection: 'column', gap: 2 }}>
          {w.pod_resources.mem_current != null ? (
            <div style={{ ...mono, color: memColor(w.pod_resources.mem_current, w.pod_resources.mem_max) }}>
              {i18nT('pages.devFleetPage.pod_memory', {
                value: w.pod_resources.mem_max != null
                  ? fmtBytes(w.pod_resources.mem_current) + ' / ' + fmtBytes(w.pod_resources.mem_max)
                  : fmtBytes(w.pod_resources.mem_current),
              })}
            </div>
          ) : null}
          {w.pod_resources.cpu_pct != null ? (
            <div style={{ ...mono, color: 'var(--text)' }}>{i18nT('pages.devFleetPage.pod_cpu', { value: fmtPercent(w.pod_resources.cpu_pct / 100, { maximumFractionDigits: 1 }) })}</div>
          ) : null}
          {w.pod_resources.tasks != null ? (
            <div style={{ ...mono, color: 'var(--text)' }}>{i18nT('pages.devFleetPage.pod_tasks', { value: w.pod_resources.tasks })}</div>
          ) : null}
          {w.pod_resources.home_bytes != null ? (
            <div style={{ ...mono, color: 'var(--text)' }}>{i18nT('pages.devFleetPage.pod_home_size', { value: fmtBytes(w.pod_resources.home_bytes) })}</div>
          ) : null}
        </div>
      ) : null}
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        {d.pod_running ? (
          <Btn onClick={() => { if (!logsOpen) { setLogsOpen(true); onLoadLogs() } else setLogsOpen(false) }} disabled={!!logsLoading}>
            {iconLabel(<FileText size={12} className="lucide-inline" />, logsLoading ? i18nT('pages.devFleetPage.loading') : logsOpen ? i18nT('pages.devFleetPage.hide_logs') : i18nT('pages.devFleetPage.load_pod_logs'))}
          </Btn>
        ) : null}
        {!w.is_main ? (
          <Btn danger onClick={onRemove} disabled={!!busy[w.name + ':remove']}>
            {iconLabel(<Trash2 size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.remove'))}
          </Btn>
        ) : null}
      </div>
      {logsOpen && logs ? (
        <pre style={{ margin: '4px 0 0', padding: '8px 10px', maxHeight: 200, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{logs}</pre>
      ) : null}
    </div>
  )
}

/** A worktree row's action buttons, its row menu and the rebase result chip. */
export function DevFleetRowActions({
  w, fleet, busy, rebaseResult, syncRun, gatewayMutating, liveStateUnknown, stagedWorktree, liveWorktree,
  dismissRebaseResult, act, launchQa, provision, syncMain, rebaseWorktree, restartGateway, makeLive,
  podsAvailable,
}: {
  w: Worktree
  fleet: FleetData | undefined
  busy: Record<string, boolean>
  rebaseResult: Record<string, RebaseResult>
  syncRun: SyncRun | null
  gatewayMutating: boolean
  liveStateUnknown: boolean
  stagedWorktree: Worktree | null
  liveWorktree: Worktree | null
  dismissRebaseResult: (name: string) => void
  act: (name: string, kind: string) => Promise<void>
  launchQa: (name: string) => void
  provision: (name: string) => Promise<void>
  syncMain: () => Promise<void>
  rebaseWorktree: (name: string) => Promise<void>
  restartGateway: () => Promise<void>
  makeLive: (w: Worktree | UndoTarget, opts?: { undo?: boolean }) => Promise<void>
  podsAvailable: boolean
}): ReactNode[] {
  if (w.is_main) {
    const out: ReactNode[] = [
      <ConfirmBtn key="sync" title={i18nT('pages.devFleetPage.pull_build_main')} desc={fleet?.gateway_service_active ? i18nT('pages.devFleetPage.pulls_main_rebuilds_then_restarts_keep_page_open') : i18nT('pages.devFleetPage.pulls_main_and_rebuilds_6_min_does_not_restart')} confirmLabel={i18nT('pages.devFleetPage.start')} onConfirm={() => syncMain()} btn={{ disabled: !!busy['__syncmain'] || syncRun?.status === 'running' || gatewayMutating }}>
        {iconLabel(<RefreshCw size={13} className="lucide-inline" />, busy['__syncmain'] || syncRun?.status === 'running' ? i18nT('pages.devFleetPage.building') : i18nT('pages.devFleetPage.pull_build_2'))}
      </ConfirmBtn>,
    ]
    const showRestart = !!fleet?.gateway_service_active
    const showCancel = !!w.is_live && !!stagedWorktree
    if (showRestart && showCancel) {
      // Both gateway actions at once (reachable on a foreground-eligible
      // host with a stage pending) would put a third sibling beside
      // Pull+Build and break the two-button row cap — collapse them into
      // one overflow trigger, which counts as a single control.
      out.push(
        <MenuBtn key="gwmenu" items={[
          { label: i18nT('pages.devFleetPage.restart'), icon: <RotateCw size={13} className="lucide-inline" />, onClick: () => restartGateway(), disabled: gatewayMutating },
          { label: i18nT('pages.devFleetPage.cancel_staged_cutover'), icon: <X size={13} className="lucide-inline" />, onClick: () => makeLive(w), disabled: gatewayMutating, title: i18nT('pages.devFleetPage.keeps_this_checkout_the_live_target_and_discards', { staged: stagedWorktree?.name ?? '' }) },
        ]} />
      )
    } else {
      if (showRestart) {
        out.push(
          <Btn key="restart" onClick={() => restartGateway()} disabled={gatewayMutating} aria-label={i18nT('pages.devFleetPage.restart_gateway')}>
            {iconLabel(<RotateCw size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.restart'))}
          </Btn>
        )
      }
      // After a cutover to a feature worktree, main is dormant (is_live=false)
      // and this inline control is the only way back to running main live. It sits
      // OUTSIDE the gateway_service_active gate on purpose: staging a cutover
      // needs no drivable service, and a host without one is precisely where
      // gating it would strand the operator on a feature worktree with no route
      // back. Consistent with makeLive()'s guard: shown iff the row is NOT live.
      if (!w.is_live && !w.is_staged) {
        out.push(
          <Btn
            key="makelive"
            onClick={() => makeLive(w)}
            disabled={gatewayMutating || liveStateUnknown}
            title={liveStateUnknown ? i18nT('pages.devFleetPage.live_state_unknown') : i18nT('pages.devFleetPage.repoint_the_live_gateway_back_at_main_restarts_t')}
            data-testid={liveStateUnknown ? 'fleet-make-live-disabled-unknown' : undefined}
          >
            {iconLabel(<Rocket size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.make_live'))}
          </Btn>
        )
      }
      // Mutually exclusive with Make live: this appears only while THIS row is
      // live and a cutover is staged onto another worktree. Same makeLive()
      // call — re-confirming the running checkout as the live target is the
      // cancel — and it sits outside the gateway_service_active gate for the
      // same reason Make live does: staged cutovers exist precisely on hosts
      // without a drivable service.
      if (showCancel) {
        out.push(
          <Btn key="cancelcutover" onClick={() => makeLive(w)} disabled={gatewayMutating} title={i18nT('pages.devFleetPage.keeps_this_checkout_the_live_target_and_discards', { staged: stagedWorktree?.name ?? '' })}>
            {iconLabel(<X size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.cancel_staged_cutover'))}
          </Btn>
        )
      }
    }
    if (fleet?.build_pending) {
      // Keep the visible text short: the ACTIONS grid column is fixed-width and
      // the Badge pill is whitespace-nowrap, so long text overflows leftward
      // into the UPDATED/BEHIND columns. Full instruction lives in the tooltip.
      out.push(<Badge key="bp" variant="warn" title={i18nT('pages.devFleetPage.build_pending_restart_gateway_to_apply_kirocrew')}>{i18nT('pages.devFleetPage.build_pending')}</Badge>)
    }
    return out
  }
  const out: ReactNode[] = []
  if (!w.has_dist) {
    // Active/failed provisioning is rendered as a row-spanning stepper (see
    // DevFleetProvStepper), so this branch only offers the entry-point button.
    out.push(<Btn key="prov" onClick={() => provision(w.name)}>{i18nT('pages.devFleetPage.provision')}</Btn>)
  } else if (w.running) {
    out.push(<Btn key="open" onClick={() => act(w.name, 'open')}>{iconLabel(<ExternalLink size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.open'))}</Btn>)
  }
  const podBusy = busy[w.name + ':up'] || busy[w.name + ':down'] || busy[w.name + ':restart']
  if (podBusy) out.push(<span key="podbusy" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--muted)' } as CSSProperties}><LoaderCircle size={12} className="lucide-inline animate-spin" /> {i18nT('pages.devFleetPage.pod')}{"\u2026"}</span>)
  out.push(<MenuBtn key="menu" items={[
    podsAvailable && w.has_dist && !w.running ? { label: i18nT('pages.devFleetPage.spin_up_pod'), icon: <Play size={13} className="lucide-inline" />, onClick: () => act(w.name, 'up') } : null,
    podsAvailable && w.running ? { label: i18nT('pages.devFleetPage.restart_pod'), icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => act(w.name, 'restart') } : null,
    { label: i18nT('pages.devFleetPage.rebase_onto_main'), icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => rebaseWorktree(w.name), disabled: !!busy[w.name + ':rebase'] },
    // Staging a cutover writes only the live-target pointer, so it needs no
    // pod support and no drivable service — gating it on podsAvailable would
    // hide it on exactly the hosts it exists to serve.
    // Hidden on the already-staged row: there it only re-stages, and next
    // to Cancel staged cutover it misreads as "complete the cutover now".
    !w.is_live && !w.is_staged ? { label: i18nT('pages.devFleetPage.make_live'), icon: <Rocket size={13} className="lucide-inline" />, onClick: () => makeLive(w), disabled: gatewayMutating || liveStateUnknown, title: liveStateUnknown ? i18nT('pages.devFleetPage.live_state_unknown') : i18nT('pages.devFleetPage.repoint_the_live_gateway_at_this_worktree_restar') } : null,
    // The cancel counterpart: only while THIS row is live and a cutover is
    // staged onto another worktree. Ungated on podsAvailable for the same
    // reason as Make live — cancelling touches only the live-target pointer.
    w.is_live && stagedWorktree ? { label: i18nT('pages.devFleetPage.cancel_staged_cutover'), icon: <X size={13} className="lucide-inline" />, onClick: () => makeLive(w), disabled: gatewayMutating, title: i18nT('pages.devFleetPage.keeps_this_checkout_the_live_target_and_discards', { staged: stagedWorktree.name }) } : null,
    // The same cancel, co-located with the state that prompts it: the STAGED
    // row wears the "Restart pending" badge, so it is where an operator who
    // staged the wrong worktree actually looks. Still cancels by confirming
    // the LIVE worktree — the item just lives where the problem is visible.
    !w.is_live && stagedWorktree?.name === w.name && liveWorktree ? { label: i18nT('pages.devFleetPage.cancel_staged_cutover'), icon: <X size={13} className="lucide-inline" />, onClick: () => makeLive(liveWorktree), disabled: gatewayMutating, title: i18nT('pages.devFleetPage.keeps_the_live_checkout_the_live_target_and_disc', { staged: stagedWorktree.name }) } : null,
    // QA + video drives the pod-e2e suite, which brings a pod up.
    podsAvailable ? { label: i18nT('pages.devFleetPage.qa_video'), icon: <Video size={13} className="lucide-inline" />, onClick: () => launchQa(w.name) } : null,
    podsAvailable && w.running ? { label: i18nT('pages.devFleetPage.stop_pod'), icon: <Square size={13} className="lucide-inline" />, onClick: () => act(w.name, 'down'), danger: true } : null,
  ]} />)
  const rr = rebaseResult[w.name]
  // Success keeps its click-to-dismiss chip here. An error or a conflict is
  // rendered by `renderRebaseFailure` in its own row-spanning block below the
  // action row: the notice carries two controls (hand-off + dismiss), which
  // would push this group past max-two-buttons-per-row.
  if (rr?.kind === 'ok') {
    out.push(<Clickable key="rr" aria-label={i18nT('pages.devFleetPage.dismiss')} onClick={() => dismissRebaseResult(w.name)} style={{ fontSize: 11, color: 'var(--ok)', cursor: 'pointer', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', background: 'none', border: 'none', padding: 0 } as CSSProperties}>{rr.text}</Clickable>)
  }
  return out
}

/* ─── Phase stepper (inline at main row) ─── */
/** The main row's Pull+Build stepper: running, done or failed. */
export function DevFleetSyncStepper({
  syncRun, syncLogOpen, setSyncLogOpen, dismissSync,
}: {
  syncRun: SyncRun | null
  syncLogOpen: boolean
  setSyncLogOpen: Dispatch<SetStateAction<boolean>>
  dismissSync: (rid?: string) => void
}) {
  if (!syncRun) return null
  const mono: CSSProperties = { fontFamily: 'ui-monospace, monospace', fontVariantNumeric: 'tabular-nums', fontSize: 11, color: 'var(--muted)' }
  if (syncRun.status === 'running') {
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 } as CSSProperties}>
        {/* Indeterminate by design: role=progressbar with no aria-valuenow is
            the ARIA form for "in progress, amount unknown". */}
        <LoaderCircle role="progressbar" aria-label={i18nT('pages.devFleetPage.sync_progress')} className="lucide-inline animate-spin" style={{ color: 'var(--accent)', flexShrink: 0 } as CSSProperties} />
        <span style={{ fontSize: 11, fontWeight: 600, flexShrink: 0 }}>{i18nT('pages.devFleetPage.syncing')}</span>
        {syncRun.stepLabel ? <span style={{ ...mono, flexShrink: 0 } as CSSProperties} title={i18nT('pages.devFleetPage.current_step')}>{syncRun.stepLabel}</span> : null}
        <span style={{ flex: 1, minWidth: 0 }} />
        <span style={mono}>{fmtElapsed(Date.now() - syncRun.startedAt)}</span>
        <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
        <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
      </div>
    )
  }
  if (syncRun.status === 'done') {
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.synced')}</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.restart_gateway_to_apply_the_new_build')}</span>
        <span style={{ flex: 1 }} />
        <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
        <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
      </div>
    )
  }
  // error
  return (
    <div style={{ gridColumn: '4 / -1', display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'flex-end', gap: 8, minWidth: 0 } as CSSProperties}>
      {/* A gateway-composed diagnosis (lastIsCause) is the one line the user
          has to act on, so it is the notice's message and wraps in full. When
          the backend gave no diagnosis the message is the failing step's
          stderr tail (or, from a gateway that emits no `::steperr::` markers,
          the raw log tail) — the full
          log is one click away in the Log panel. Inputs are all in git,
          so the hand-off loses nothing. The notice takes its own line
          (`basis-full`) so the Log / dismiss pair below it stays a two-control
          row (max-two-buttons-per-row).
          `whitespace-pre-wrap` on the message only: a stderr tail is several
          lines and git's is indented to associate paths with their headline,
          so collapsing it would run "would be overwritten by merge:" straight
          into the file name. The inline variant does not pre-wrap by default
          and must not start doing so for every other consumer. */}
      <ErrorNotice
        title={i18nT('pages.devFleetPage.pull_build_failed')}
        message={syncRun.last}
        messageClassName="whitespace-pre-wrap"
        variant="inline"
        askAgent
        className="basis-full min-w-0 flex-wrap select-text"
        testId="sync-error"
      />
      <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
      <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
    </div>
  )
}

/* ─── Provision stepper (inline at a worktree row) ─── */
/** A worktree row's provision stepper: starting, running, done or failed. */
export function DevFleetProvStepper({
  w, prov, provLogOpen, dismissProv, toggleProvLog,
}: {
  w: Worktree
  prov: Record<string, ProvRun | null>
  provLogOpen: Record<string, boolean>
  dismissProv: (name: string) => Promise<void>
  toggleProvLog: (name: string) => void
}) {
  const pr = prov[w.name]
  if (!pr) return null
  const mono: CSSProperties = { fontFamily: 'ui-monospace, monospace', fontVariantNumeric: 'tabular-nums', fontSize: 11, color: 'var(--muted)' }
  const open = !!provLogOpen[w.name]
  const logToggle = (
    <Clickable aria-label={i18nT('pages.devFleetPage.toggle_provision_log')} onClick={() => toggleProvLog(w.name)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{open ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
  )
  if (pr.failed) {
    // Failed action, inputs all on disk (the worktree) — hand-off on. The
    // failing step's stderr tail is the message (or, from a gateway whose
    // provision emits no `::steperr::` markers, the raw log tail — see
    // `syncFailureTail` for why the last line alone names a progress line);
    // the full log stays one click away via `logToggle`, which sits on its own
    // line under the notice: the notice already carries two controls
    // (hand-off + dismiss), the row's cap (max-two-buttons-per-row).
    // `whitespace-pre-wrap` on the message only: a stderr tail is several
    // lines, and collapsing them would run pip's headline into its remedy.
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'flex-end', gap: 8, minWidth: 0 } as CSSProperties}>
        <ErrorNotice
          title={pr.exit != null ? i18nT('pages.devFleetPage.provision_failed_exit_code', { code: pr.exit }) : i18nT('pages.devFleetPage.provision_failed')}
          message={syncFailureTail(pr.lines) || i18nT('pages.devFleetPage.provision_failed')}
          messageClassName="whitespace-pre-wrap"
          variant="inline"
          askAgent
          onDismiss={() => { void dismissProv(w.name) }}
          className="basis-full min-w-0 flex-wrap"
          testId={`provision-error-${w.name}`}
        />
        {logToggle}
      </div>
    )
  }
  if (pr.done) {
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.provisioned')}</span>
      </div>
    )
  }
  // starting / running
  const phase = provPhase(pr.lines)
  const last = lastLine(pr.lines)
  return (
    <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 } as CSSProperties}>
      <LoaderCircle size={12} className="lucide-inline animate-spin" style={{ color: 'var(--accent)', flexShrink: 0 } as CSSProperties} />
      <span style={{ fontSize: 11, fontWeight: 600, flexShrink: 0 }}>{i18nT('pages.devFleetPage.provisioning')}</span>
      {phase ? <span style={{ fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--accent)', background: 'var(--accent-subtle, rgba(99,102,241,0.14))', borderRadius: 5, padding: '1px 6px', flexShrink: 0 } as CSSProperties}>{phase}</span> : null}
      <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 } as CSSProperties} title={last}>{last || i18nT('pages.devFleetPage.starting')}</span>
      <span style={mono}>{fmtElapsed(Date.now() - pr.startedAt)}</span>
      {logToggle}
    </div>
  )
}
