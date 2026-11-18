/**
 * Settings -> Remote Crew -> My Crews: one switchable crew row. Collapsed it
 * shows the name, status, source and any blocking tag, with one main action for
 * its state; its chevron opens the details (target, port, the on/off switch,
 * who manages it, why it needs attention) and the other actions, which replace
 * the old overflow menu. Also the inline edit form and the turn-API URL field a
 * connected fargate crew shows. One of the owners `RemoteCrewPanel.tsx`
 * composes; the facade re-exports `TurnUrlField`.
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  Server,
  Rocket,
  Plug,
  Unplug,
  Trash2,
  RefreshCw,
  Stethoscope,
  Copy,
  Check,
  Power,
  MoreHorizontal,
  Pencil,
  Play,
  Cloud,
  KeyRound,
  ChevronRight,
} from 'lucide-react'
import {
  type InstanceView,
  type LaunchJob,
  type CloudCoords,
} from '../../api/client'
import {
  BUILTIN_PROVISIONER_ID,
  hasDashboardPane,
  shortenEcsTarget,
  usesSsmTransport,
} from '../../utils/remoteCrew'
import { Btn, Badge, IconButton, Toggle } from '../../components/ui'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import { getCrewRowActions } from '../../components/remoteCrewRowActions'
import { findReport, type ErrorReport } from '../../utils/errorReport'
import { copyToClipboard } from '../../utils/clipboard'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { guideCaution } from '../../guide/trustRoot'
import { StatusBadge } from './InstancesPanel'
import CrewVersionStatus, { useCrewUpdateShown } from './CrewVersionStatus'
import {
  EditInstanceForm,
  type InstanceDraft,
} from './InstanceFormFields'
import type { SigninNotice } from './remoteCrewPanelModel'
import { SigninPromptBlock } from './RemoteCrewPanelLaunch'

const connectionTypeLabel = (inst: InstanceView): string =>
  inst.connection_method === 'fargate'
    ? i18nT('pages.settings.remoteCrewPanel.type_fargate')
    : inst.connection_method === 'ssm'
      ? i18nT('pages.settings.remoteCrewPanel.type_ssm')
      : i18nT('pages.settings.remoteCrewPanel.type_ssh')

// The badges compress to acronyms (EC2 / SSM / SSH) a first-time reader may
// not know; the hover title spells out what each one means.
const connectionTypeHint = (inst: InstanceView): string =>
  inst.connection_method === 'fargate'
    ? i18nT('pages.settings.remoteCrewPanel.transport_hint_fargate')
    : inst.connection_method === 'ssm'
      ? i18nT('pages.settings.remoteCrewPanel.transport_hint_ssm')
      : i18nT('pages.settings.remoteCrewPanel.transport_hint_ssh')

/**
 * What a connected fargate crew offers instead of a dashboard: the loopback
 * URL of its turn API through the open forward, with a copy control. There
 * is deliberately no Open button. The URL answers JSON, so a browser tab on
 * it is a wall of text, and a button that promised a dashboard would be the
 * defect this field replaces.
 */
export function TurnUrlField({ url, crewName }: { url: string; crewName: string }) {
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  // Same scrollbar-none field as WebhooksPage's CopyField: a hidden scrollbar
  // leaves a long URL's clipped tail unsignalled, so paint the edge cue. #4074.
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLElement>()
  useEffect(() => { remeasure() }, [url, remeasure])
  const label = i18nT('pages.settings.remoteCrewPanel.copy_turn_url', { name: crewName })
  const handleCopy = async () => {
    if (await copyToClipboard(url)) {
      setCopyFailed(false)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } else {
      setCopyFailed(true)
    }
  }
  return (
    <div className="mt-2" data-testid="turn-url">
      <div className="text-[11px] uppercase tracking-[.08em] text-muted mb-1">
        {i18nT('pages.settings.remoteCrewPanel.turn_api')}
      </div>
      <div className="flex items-center gap-2 bg-bg-elevated border border-border rounded-md pl-3 pr-1.5 py-1.5">
        {/* Wrapper for the edge cues: the fades anchor to this non-scrolling
            parent, not the scrolled code element. from-bg-elevated matches the
            field surface. */}
        <div className="relative flex-1 min-w-0">
          <code ref={attachEdges} className="block min-w-0 font-mono text-[12px] overflow-x-auto whitespace-nowrap scrollbar-none text-card-fg">
            {url}
          </code>
          {edges.left && (
            <div aria-hidden="true" data-testid="turn-url-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-4 z-10 bg-gradient-to-r from-bg-elevated to-transparent" />
          )}
          {edges.right && (
            <div aria-hidden="true" data-testid="turn-url-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-4 z-10 bg-gradient-to-l from-bg-elevated to-transparent" />
          )}
        </div>
        <IconButton aria-label={label} onClick={handleCopy} title={label}>
          {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
        </IconButton>
      </div>
      {/* No hand-off: the remedy ("select the text and copy it manually")
          is complete on its own, and the URL stays on screen in the code
          element above. An agent cannot supply a clipboard the browser
          refused; the hand-off navigates to chat and unmounts the row that
          holds the URL. */}
      {copyFailed ? (
        <ErrorNotice
          variant="inline"
          className="mt-1.5"
          message={i18nT('pages.settings.remoteCrewPanel.copy_failed')}
          testId="turn-url-copy-error"
        />
      ) : null}
      <p className="text-[12px] text-muted mt-1">
        {i18nT('pages.settings.remoteCrewPanel.turn_url_note')}
      </p>
    </div>
  )
}

/** The AWS coordinates a lifecycle call needs, taken from the crew itself.
 *
 *  A crew launched under a non-default profile or region is invisible to the
 *  gateway's defaults, so stop/start/destroy must carry them; destroy also needs
 *  the instance id so the local registration goes away with the stack.
 */
const coordsOf = (inst: InstanceView): CloudCoords => ({
  profile: inst.aws_profile || undefined,
  region: inst.aws_region || undefined,
  instanceId: inst.ssm_target || undefined,
})

/** One switchable crew — a cloud-launched instance (Stop / Delete by tag) or a
 *  hand-added machine (Remove). */
/** The one line that says what a crew's on/off switch does; every switch points at it. */
export const CREW_SWITCH_HINT_ID = 'remote-crew-switch-hint'

/** The "on but not active yet" banner that mutes Connect. The disabled Connect
 *  button points its `aria-describedby` here, so a keyboard or screen-reader
 *  user gets the restart reason a hover `title` on a disabled button never
 *  surfaces. */
export const CREW_NEEDS_RESTART_BANNER_ID = 'remote-crew-needs-restart'

/** The notice for a contributed row-action component that threw. */
function rowActionsFailed(name: string, error: Error): string {
  return i18nT('pages.settings.remoteCrewPanel.row_actions_failed', { name, error: error.message || String(error) })
}

/**
 * The journal entry the boundary wrote for this crash, so the hand-off carries
 * its stack and scope rather than only the sentence on screen. The boundary
 * journals in `componentDidCatch`, which runs after its fallback first renders,
 * so the lookup waits for the commit.
 */
function useCrashReport(error: Error): ErrorReport | undefined {
  const [report, setReport] = useState<ErrorReport | undefined>(undefined)
  useEffect(() => {
    setReport(findReport(error.message || error.name))
  }, [error])
  return report
}

/** In the overflow menu, where the contribution's items were expected. */
function RowActionsMenuCrash({ error, inst }: { error: Error; inst: InstanceView }) {
  const report = useCrashReport(error)
  const message = rowActionsFailed(inst.name, error)
  const id = `crew-row-actions-error-${inst.id}`
  return (
    <>
      <DropdownMenuSeparator />
      <ErrorNotice id={id} variant="inline" className="px-2 py-1.5" message={message} report={report} />
      <ErrorNoticeMenuItem Item={DropdownMenuItem} message={message} describedBy={id} report={report} />
    </>
  )
}

/** Under the row header. Hand-off on: the row holds no draft here (the edit form is its own subtree). */
function RowActionsDetailCrash({ error, name }: { error: Error; name: string }) {
  const report = useCrashReport(error)
  return <ErrorNotice variant="inline" className="mt-2" askAgent message={rowActionsFailed(name, error)} report={report} />
}

export function CrewRow({
  inst,
  cloudTag,
  busy,
  deleting,
  confirmDelete,
  confirmRemove,
  connectBlocked,
  onConnect,
  onOpen,
  onDisconnect,
  onSetEnabled,
  onDiagnose,
  onRemove,
  onStop,
  onStart,
  onDelete,
  onRequestDelete,
  onRequestRemove,
  onEdit,
  onEditSaved,
  editDraft,
  onEditDraftChange,
  editExternallyChanged,
  editDraftSeq,
  onEditRebase,
  editing,
  blocked,
  signinJob,
  onRestartSignin,
  restartingSignin,
  onFetchSignin,
  fetchingSignin,
  signinNotice,
  signinCheckError = null,
}: {
  inst: InstanceView
  cloudTag: string | null
  busy: string
  deleting: boolean
  confirmDelete: boolean
  confirmRemove: boolean
  /** The gateway was enabled but not yet restarted, so connecting cannot work
   *  until it restarts (the same `active === false` state that shows the
   *  "not active yet" banner). Mutes Connect so the button agrees with the
   *  banner instead of looking fully clickable. */
  connectBlocked: boolean
  onConnect: (id: string) => void
  /** Switch the dashboard to this crew's pane, as its switcher entry does. */
  onOpen: (id: string) => void
  onDisconnect: (id: string) => void
  onSetEnabled: (id: string, enabled: boolean) => void
  onDiagnose: (id: string) => void
  onRemove: (id: string) => void
  onStop: (tag: string, coords: CloudCoords) => void
  onStart: (tag: string, coords: CloudCoords) => void
  onDelete: (tag: string, coords: CloudCoords) => void
  onRequestDelete: (tag: string | null) => void
  onRequestRemove: (id: string | null) => void
  onEdit: (id: string | null) => void
  onEditSaved: (updated: InstanceView) => void
  /** Unsaved work for THIS crew, held by the panel so it survives unmount. */
  editDraft: InstanceDraft | null
  onEditDraftChange: (draft: InstanceDraft | null) => void
  /** Persisted fields that moved under the open draft (see EditInstanceForm). */
  editExternallyChanged: string[]
  /** Bumped when the draft is rebased, so the form remounts and re-seeds. */
  editDraftSeq: number
  onEditRebase: () => void
  editing: boolean
  /** This row's Edit was refused because another row holds unsaved changes. */
  blocked: boolean
  /** The launch job for this crew when its Kiro sign-in is missing or in
   *  progress; null when signed in (or not a cloud crew). */
  signinJob: LaunchJob | null
  onRestartSignin: (id: string) => void
  restartingSignin: boolean
  onFetchSignin: (id: string) => void
  fetchingSignin: boolean
  signinNotice: SigninNotice | null
  /** Why the gateway could not ask this crew's box whether it is signed in, or
   *  null. Not a badge: nobody checked that its chats fail. */
  signinCheckError?: string | null
}) {
  const connected = inst.status.state === 'connected'
  const isCloud = cloudTag !== null
  // The owner stopped this cloud crew's machine from this menu. Its tunnel is
  // refused until Start, so the row reads Stopped with Start as the next step
  // rather than as a failed connection.
  const cloudStopped = isCloud && !!inst.cloud_stopped
  // An unsigned crew still connects: the user can sign in on the crew itself once
  // connected. The badge and the sign-in controls stay so the missing sign-in is
  // visible, but they never hold Connect back.
  //
  // NOT gated on `!connected`: auto-connect is default-on, so an unsigned crew is
  // routinely connected already — and that is precisely when the badge and the
  // recovery controls are needed, because the chats are the thing that fails.
  const awaitingSignin = signinJob !== null
  // Two persisted signals mark a row possibly-cloud when no launch job matches: an
  // EC2 stamp (`provisioner_id`), and an SSM target — the CLI launcher registers real
  // cloud crews the same way, and those never produce a launch job in this gateway's
  // store. Calling either "added by you" would invite a Remove that unregisters a
  // live, billing instance and takes away the only place the dashboard could still
  // delete it. We cannot prove which it is, so treat it as possibly-cloud: same
  // confirm step, and copy that says what Remove does and does not do.
  const unverifiedCloud =
    !isCloud &&
    (inst.provisioner_id === BUILTIN_PROVISIONER_ID ||
      (usesSsmTransport(inst) && !!inst.ssm_target))
  // A stop/start this row asked for is still in flight.
  const lifecycleBusy = busy === `stop:${cloudTag}` || busy === `start:${cloudTag}`
  // States that occupy the row's second control slot with an inline button.
  const transient =
    deleting || lifecycleBusy || (isCloud && confirmDelete) || (!isCloud && confirmRemove)
  const target = usesSsmTransport(inst) ? inst.ssm_target : inst.ssh_host
  // A CHAINED row's host is a RECORD, not a target. It arrives in the announcing
  // pane's payload and this gateway never dials it: `_resolve_transport` returns the
  // PARENT's host for any row carrying `via_instance_id`, so the row's own `ssh_host`
  // and `remote_port` are never reached. Rendering them where a verified target goes
  // let an untrusted string borrow that authority -- a compromised pane could make a
  // row read like a production database while reaching nothing. So they are labelled
  // as reported, and the port is dropped: it is the crew's own gateway port, which is
  // not what this dashboard forwards to either.
  const reportedOnly = !!inst.via_instance_id
  // A fargate crew has no dashboard; while its forward is up, the card shows
  // the turn URL the status carries instead of offering something to open.
  const turnUrl = inst.connection_method === 'fargate' && connected ? inst.status?.turn_url || '' : ''
  // Lifecycle controls a contributed provisioner draws for the crews it made.
  // A tag-correlated row is the core's own EC2 crew, whose lifecycle is above.
  const rowActions = isCloud ? undefined : getCrewRowActions(inst)
  const rowActionsDisabled = !!busy || deleting || transient
  // The row's state in words. A tunnel error reads Disconnected with a
  // "Can't connect" tag; its message moves to the details.
  const failing = !inst.disabled && !cloudStopped && inst.status.state === 'error'
  const shownStatus = cloudStopped
    ? { ...inst.status, state: 'stopped' as const, error: undefined }
    : { ...inst.status, state: failing ? 'disconnected' as const : inst.status.state, error: undefined }
  const updateShown = useCrewUpdateShown(inst.id, connected && !inst.disabled)
  // A row that needs sign-in opens with its sign-in block showing: that block
  // is the only way to finish the crew's setup. The need can be learned after
  // the first render (the box is asked first).
  // Until the reader toggles it, the row follows that need in the same render.
  const [chosen, setExpanded] = useState<boolean | null>(null)
  const expanded = chosen ?? awaitingSignin
  // Opening the editor (from the details, or a held draft coming back) shows it.
  useEffect(() => { if (editing || blocked) setExpanded(true) }, [editing, blocked])
  const detailsId = `crew-row-details-${inst.id}`
  const actionsDisabled = !!busy || deleting
  // One main action for the row's state. Every other action is in the details.
  const main: 'signin' | 'start' | 'diagnose' | 'update' | 'open' | 'connect' | null = transient
    ? null
    : cloudStopped
      // Before sign-in: a stopped crew must be running to finish signing in.
      ? 'start'
      : awaitingSignin
        ? 'signin'
        : inst.disabled
          ? null
          : failing
            ? 'diagnose'
            : connected && updateShown
              ? 'update'
              : connected
                ? (hasDashboardPane(inst) ? 'open' : null)
                : 'connect'
  const managedBy = isCloud
    ? i18nT('pages.settings.remoteCrewPanel.launched_by_kiro_crew')
    : inst.provisioner_id === BUILTIN_PROVISIONER_ID
      // An EC2-stamped row wears the EC2 badge, whose hint says it WAS
      // launched by the EC2 launcher — the caption must agree with the
      // badge, not hedge about whether AWS resources exist.
      ? i18nT('pages.settings.remoteCrewPanel.stamped_ec2_note')
      // A fargate row IS an AWS resource by definition, so it must not
      // hedge like unverifiedCloud does; it keeps the same Remove confirm
      // step but says plainly that the task keeps running after Remove.
      : inst.connection_method === 'fargate'
      ? i18nT('pages.settings.remoteCrewPanel.fargate_task_note')
      : unverifiedCloud
        ? i18nT('pages.settings.remoteCrewPanel.unverified_cloud_note')
        : `${i18nT('pages.settings.remoteCrewPanel.added_by_you')} · ${i18nT('pages.settings.remoteCrewPanel.doesnt_manage')}`
  const targetText = inst.connection_method === 'fargate'
    // Two tasks in one cluster differ only at the far right of the ECS
    // target; the short form keeps that tail visible and the title carries
    // the full target.
    ? <span title={target}>{shortenEcsTarget(target)}</span>
    : reportedOnly
      ? (
        <span title={i18nT('pages.settings.remoteCrewPanel.reported_host_hint')}>
          {i18nT('pages.settings.remoteCrewPanel.reported_host', { host: target })}
        </span>
      )
      : <>{target}{usesSsmTransport(inst) && inst.aws_region ? ` (${inst.aws_region})` : ''}</>
  const detailRow = (label: string, value: ReactNode) => (
    <div className="contents">
      <dt className="text-muted">{label}</dt>
      <dd className="m-0 text-text min-w-0 break-words">{value}</dd>
    </div>
  )
  return (
    <div className="py-2.5 border-b border-border last:border-b-0" data-crew-id={inst.id}>
    <div className="flex items-start justify-between gap-3">
      <div className="flex items-start gap-2 min-w-0">
        <IconButton
          aria-expanded={expanded}
          aria-controls={detailsId}
          aria-label={i18nT(expanded ? 'pages.settings.remoteCrewPanel.details_hide' : 'pages.settings.remoteCrewPanel.details_show', { name: inst.name })}
          onClick={() => setExpanded(!expanded)}
          className="mt-1.5 shrink-0"
        >
          <ChevronRight size={14} aria-hidden className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
        </IconButton>
        <span className={`mt-0.5 w-8 h-8 shrink-0 grid place-items-center rounded-md ${isCloud ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>
          {isCloud ? <Rocket size={16} /> : <Server size={16} />}
        </span>
        <div className="min-w-0">
          <div className="text-text-strong text-sm font-medium truncate">{inst.name}</div>
          <div className="mt-0.5 flex items-center gap-1.5 flex-wrap text-[12px] text-muted">
            {inst.disabled
              ? <span>{i18nT('pages.settings.remoteCrewPanel.crew_disabled')}</span>
              : <StatusBadge status={shownStatus} />}
            <span aria-hidden>·</span>
            {(inst.provisioner_id === BUILTIN_PROVISIONER_ID || isCloud) && (
              <Badge
                variant="aim"
                title={i18nT('pages.settings.remoteCrewPanel.source_ec2_hint')}
                aria-label={i18nT('pages.settings.remoteCrewPanel.source_ec2_hint')}
              >
                <Cloud className="lucide-inline" />
                {i18nT('pages.settings.remoteCrewPanel.source_ec2')}
              </Badge>
            )}
            <Badge variant="muted" title={connectionTypeHint(inst)} aria-label={connectionTypeHint(inst)}>
              {connectionTypeLabel(inst)}
            </Badge>
            {awaitingSignin && (
              <Badge variant="warn" title={i18nT('pages.settings.remoteCrewPanel.needs_sign_in_hint')}>
                <KeyRound className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.needs_sign_in')}
              </Badge>
            )}
            {failing && (
              <Badge variant="err" title={inst.status.error || undefined}>
                {i18nT('pages.settings.remoteCrewPanel.cant_connect')}
              </Badge>
            )}
            <CrewVersionStatus instanceId={inst.id} name={inst.name} connected={connected && !inst.disabled} part="badge" />
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
        {main === 'signin' ? (
          // The sign-in block is in the details; this opens them on it.
          <Btn primary onClick={() => setExpanded(true)} aria-label={i18nT('pages.settings.remoteCrewPanel.sign_in_crew', { name: inst.name })}>
            <KeyRound className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.sign_in')}
          </Btn>
        ) : main === 'start' && cloudTag !== null ? (
          <Btn
            primary
            onClick={() => onStart(cloudTag, coordsOf(inst))}
            disabled={actionsDisabled}
            aria-label={i18nT('pages.settings.remoteCrewPanel.start_crew', { name: inst.name })}
          >
            <Play className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.start')}
          </Btn>
        ) : main === 'diagnose' ? (
          <Btn primary onClick={() => onDiagnose(inst.id)} disabled={actionsDisabled} aria-label={i18nT('pages.settings.instancesPanel.diagnose_2', { name: inst.name })}>
            <Stethoscope className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.diagnose')}
          </Btn>
        ) : main === 'update' ? (
          <CrewVersionStatus instanceId={inst.id} name={inst.name} connected part="control" />
        ) : main === 'open' ? (
          <>
            {/* A finished update's outcome, if one is cached; nothing otherwise. */}
            <CrewVersionStatus instanceId={inst.id} name={inst.name} connected part="control" />
            <Btn primary onClick={() => onOpen(inst.id)} disabled={actionsDisabled} aria-label={i18nT('pages.settings.remoteCrewPanel.open_crew', { name: inst.name })}>
              {i18nT('pages.settings.remoteCrewPanel.open')}
            </Btn>
          </>
        ) : main === 'connect' ? (
          <Btn
            primary
            onClick={() => onConnect(inst.id)}
            disabled={actionsDisabled || connectBlocked}
            // A `title` on a disabled button is unreliable: many browsers never
            // show it on hover, and it is not keyboard- or screen-reader-reachable.
            // While connecting is blocked, point the button at the restart banner
            // so the reason is in its accessible description too.
            title={connectBlocked ? i18nT('pages.settings.instancesPanel.connect_disabled_needs_restart') : undefined}
            aria-describedby={connectBlocked ? CREW_NEEDS_RESTART_BANNER_ID : undefined}
          >
            <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? i18nT('pages.settings.instancesPanel.connecting') : i18nT('pages.settings.instancesPanel.connect')}
          </Btn>
        ) : null}
        {/* A teardown and a pending confirmation stay on the row itself: both
            are transient states the user must see without opening anything —
            the delete only requested the teardown, and AWS confirms minutes
            later when the row is dropped. Hiding that read as "nothing happened". */}
        {deleting ? (
          <Btn danger disabled aria-label={i18nT('pages.settings.remoteCrewPanel.deleting')}>
            <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.deleting')}
          </Btn>
        ) : lifecycleBusy ? (
          // Report the progress on the row under the SAME accessible name the
          // action carried, so the crew a request belongs to is never ambiguous.
          <Btn
            disabled
            aria-label={
              busy === `stop:${cloudTag}`
                ? i18nT('pages.settings.remoteCrewPanel.stop_crew', { name: inst.name })
                : i18nT('pages.settings.remoteCrewPanel.start_crew', { name: inst.name })
            }
          >
            <RefreshCw className="lucide-inline animate-spin" />{' '}
            {busy === `stop:${cloudTag}`
              ? i18nT('pages.settings.remoteCrewPanel.stopping')
              : i18nT('pages.settings.remoteCrewPanel.starting')}
          </Btn>
        ) : isCloud && confirmDelete ? (
          <>
            <Btn danger onClick={() => onDelete(cloudTag, coordsOf(inst))} disabled={!!busy} aria-label={i18nT('pages.settings.remoteCrewPanel.confirm_delete_of', { name: inst.name })} {...guideCaution}>
              {/* Names its target on screen, not only to assistive tech: this click
                  terminates an EC2 instance, and "Confirm delete" beside two other
                  rows does not say WHICH. */}
              <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.delete_crew', { name: inst.name })}
            </Btn>
            {/* An armed destructive button needs a way out, or a mis-click leaves
                the row showing nothing but a button that terminates an EC2 instance. */}
            <Btn onClick={() => onRequestDelete(null)} disabled={!!busy}>
              {i18nT('pages.settings.remoteCrewPanel.cancel')}
            </Btn>
          </>
        ) : !isCloud && confirmRemove ? (
          <>
            <Btn danger onClick={() => onRemove(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
              <Trash2 className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}
            </Btn>
            <Btn onClick={() => onRequestRemove(null)} disabled={!!busy}>
              {i18nT('pages.settings.remoteCrewPanel.cancel')}
            </Btn>
          </>
        ) : null}
      </div>
    </div>
    {/* Below the row header, and shown collapsed too: why the gateway could not
        ask this crew's box whether it is signed in. Not a badge: nobody checked
        that its chats fail. */}
    {signinCheckError && (
      <ErrorNotice variant="inline" impact="minor" className="mt-2" askAgent message={signinCheckError} />
    )}
    {expanded && (
      <div id={detailsId} className="mt-2 ml-[4.25rem] text-[12px]" data-testid="crew-row-details">
        <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
          {detailRow(
            usesSsmTransport(inst)
              ? i18nT('pages.settings.remoteCrewPanel.detail_instance')
              : i18nT('pages.settings.remoteCrewPanel.detail_host'),
            targetText,
          )}
          {/* A chained row's port is the crew's own gateway port, not what
              this dashboard forwards to, so it is not shown as a target. */}
          {!reportedOnly && detailRow(i18nT('pages.settings.remoteCrewPanel.detail_port'), inst.remote_port)}
          {/* The switch's own text names it ("Connections: On"), so it spans the
              row rather than repeating the word in a label cell. */}
          <div className="col-span-2 inline-flex items-center gap-1.5 text-text">
            <Toggle checked={!inst.disabled} onChange={on => onSetEnabled(inst.id, on)}
              disabled={actionsDisabled} describedBy={CREW_SWITCH_HINT_ID}
              label={i18nT('pages.settings.remoteCrewPanel.crew_enabled_named', { name: inst.name })} />
            {inst.disabled
              ? i18nT('pages.settings.remoteCrewPanel.crew_disabled')
              : i18nT('pages.settings.remoteCrewPanel.crew_enabled')}
          </div>
          {detailRow(i18nT('pages.settings.remoteCrewPanel.detail_managed_by'), managedBy)}
          {(failing || cloudStopped || awaitingSignin) && detailRow(
            i18nT('pages.settings.remoteCrewPanel.detail_why'),
            failing
              // The tunnel's own error. `askAgent` is safe: every draft this
              // page holds lives in the store, so the hand-off loses nothing.
              // `flex-wrap`: a long error pushes the hand-off onto its own line
              // instead of past the card's edge.
              ? <ErrorNotice variant="inline" message={inst.status.error} askAgent className="flex-wrap" />
              : cloudStopped
                ? i18nT('pages.settings.remoteCrewPanel.cloud_stopped_note')
                : i18nT('pages.settings.remoteCrewPanel.needs_sign_in_hint'),
          )}
        </dl>
        {rowActions?.detail && (
          <ErrorBoundary
            scope={`crew-row-detail:${rowActions.provisionerId}`}
            fallback={error => <RowActionsDetailCrash error={error} name={inst.name} />}
          >
            <rowActions.detail inst={inst} disabled={rowActionsDisabled} />
          </ErrorBoundary>
        )}
        {turnUrl && <TurnUrlField url={turnUrl} crewName={inst.name} />}
        {awaitingSignin && signinJob && (
          <SigninPromptBlock
            job={signinJob}
            crewName={inst.name}
            onRestart={onRestartSignin}
            restarting={restartingSignin}
            onFetch={onFetchSignin}
            fetching={fetchingSignin}
            notice={signinNotice}
            compact
          />
        )}
        {/* The row's other actions. While an armed confirm or a teardown holds
            the row they stand down, as the menu they replace did. */}
        {!transient && (
          <div className="mt-2.5 flex items-center gap-2 flex-wrap">
            {connected ? (
              <Btn onClick={() => onDisconnect(inst.id)} disabled={actionsDisabled}>
                <Unplug className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.disconnect')}
              </Btn>
            ) : main !== 'connect' && !inst.disabled && !cloudStopped ? (
              // A crew whose main action is something else (Diagnose, Sign in)
              // can still be connected from here.
              <Btn onClick={() => onConnect(inst.id)} disabled={actionsDisabled || connectBlocked}
                aria-describedby={connectBlocked ? CREW_NEEDS_RESTART_BANNER_ID : undefined}>
                <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? i18nT('pages.settings.instancesPanel.connecting') : i18nT('pages.settings.instancesPanel.connect')}
              </Btn>
            ) : null}
            {main !== 'diagnose' && (
              <Btn onClick={() => onDiagnose(inst.id)} disabled={actionsDisabled} aria-label={i18nT('pages.settings.instancesPanel.diagnose_2', { name: inst.name })}>
                <Stethoscope className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.diagnose')}
              </Btn>
            )}
            <Btn onClick={() => onEdit(inst.id)} disabled={actionsDisabled}>
              <Pencil className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.edit_settings')}
            </Btn>
            {rowActions?.menuItems && (
              // A contribution draws menu items, so it keeps a menu of its own.
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <IconButton
                    aria-label={i18nT('pages.settings.remoteCrewPanel.more_actions', { name: inst.name })}
                    disabled={actionsDisabled}
                  >
                    <MoreHorizontal className="lucide-inline" />
                  </IconButton>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[200px]">
                  <ErrorBoundary
                    scope={`crew-row-actions:${rowActions.provisionerId}`}
                    // A throw costs this crew its extra items, not the menu. Say so in
                    // the menu, where the missing items were expected.
                    fallback={error => <RowActionsMenuCrash error={error} inst={inst} />}
                  >
                    <rowActions.menuItems inst={inst} disabled={rowActionsDisabled} />
                  </ErrorBoundary>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {isCloud ? (
              <>
                {/* Stop without Start is a one-way door: a stopped crew needs a
                    path back to running while its EBS volume keeps billing. A
                    crew stopped from here carries Start as its main action. */}
                {!cloudStopped && (
                  <>
                    <Btn onClick={() => onStop(cloudTag, coordsOf(inst))} disabled={actionsDisabled}
                      aria-label={i18nT('pages.settings.remoteCrewPanel.stop_crew', { name: inst.name })}>
                      <Power className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.stop')}
                    </Btn>
                    <Btn onClick={() => onStart(cloudTag, coordsOf(inst))} disabled={actionsDisabled}
                      aria-label={i18nT('pages.settings.remoteCrewPanel.start_crew', { name: inst.name })}>
                      <Play className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.start')}
                    </Btn>
                  </>
                )}
                <Btn danger onClick={() => onRequestDelete(cloudTag)} disabled={actionsDisabled}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.delete_crew', { name: inst.name })}>
                  <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.delete')}
                </Btn>
              </>
            ) : (
              // Always confirm-gated: the label ends in an ellipsis because a
              // second step follows, and the record being removed (host, port,
              // TTL, profile) is the one this panel exists to let you correct.
              <Btn danger onClick={() => onRequestRemove(inst.id)} disabled={actionsDisabled}
                aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
                <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.remove')}
              </Btn>
            )}
          </div>
        )}
      </div>
    )}
    {blocked && (
      // At the row, and assertive: a refusal that renders anywhere else reads
      // as the click having done nothing at all.
      <p role="alert" className="mt-2 text-[12px] text-warn">
        {i18nT('pages.settings.remoteCrewPanel.finish_open_edit_first')}
      </p>
    )}
    {editing && (
      <EditInstanceForm
        key={`edit-${inst.id}-${editDraftSeq}`}
        inst={inst}
        onSaved={onEditSaved}
        onCancel={() => onEdit(null)}
        draft={editDraft}
        externallyChanged={editExternallyChanged}
        onDraftChange={onEditDraftChange}
        onRebase={onEditRebase}
        // Only a CORRELATED cloud crew is addressed by its connection identity:
        // Stop / Start / Delete resolve the machine through {profile, region,
        // ssm_target}, so editing those would leave a billing instance the
        // dashboard can no longer reach. A crew we cannot correlate is offered no
        // lifecycle action at all, so freezing its fields would protect nothing
        // and would take away a legitimate way to correct its AWS profile.
        lockTransport={isCloud}
      />
    )}
    </div>
  )
}
