/**
 * RemoteCrewPanel — Settings → Remote Crew. One page, two tabs:
 *
 *   1. "Your crews" (default) — the machines you can switch to from the top
 *      header: any in-progress cloud launch (a durable gateway job), the
 *      connected/added instances, and the add-a-machine form. Cloud-launched
 *      crews are told apart from hand-added ones by correlating each SSM
 *      instance's target id with a launch job's `instance_id`, so cloud rows can
 *      offer the cloud lifecycle (Stop / Delete-by-tag) that a plain tunnel row
 *      cannot.
 *   2. "Set up a new one" — an AWS prerequisite checklist (from the cloud
 *      preflight) and a launch form that spins up a cloud-hosted crew on the
 *      user's OWN AWS account, then a progress card that polls the launch job.
 *
 * The instance CRUD (connect / disconnect / diagnose / remove) and the
 * enable/disable gate mirror InstancesPanel; the add-existing form and the
 * StatusBadge are reused from it directly.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Server,
  Rocket,
  Plug,
  Unplug,
  Trash2,
  RefreshCw,
  Stethoscope,
  AlertTriangle,
  CheckCircle,
  Copy,
  Check,
  ExternalLink,
  ChevronDown,
  X,
  Power,
  Loader2,
} from 'lucide-react'
import {
  api,
  ApiError,
  type InstanceView,
  type LaunchJob,
  type CloudPreflight,
  type CloudCoords,
} from '../../api/client'
import { Card, Btn, Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { copyToClipboard } from '../../utils/clipboard'
import { useAppDispatch } from '../../store'
import { removeWarm } from '../../store/instancesSlice'
import { i18nT } from '../../i18n/t'
import { AddInstanceForm, StatusBadge } from './InstancesPanel'


/** A launch job the user is still waiting on (not yet a switchable crew). */
const IN_PROGRESS: LaunchJob['status'][] = ['pending', 'running', 'awaiting_signin']
const isInProgress = (j: LaunchJob) => IN_PROGRESS.includes(j.status)

/** A launch that has reached a final state — nothing more will happen to it. */
const TERMINAL: LaunchJob['status'][] = ['done', 'failed', 'cancelled']
const isTerminal = (j: LaunchJob) => TERMINAL.includes(j.status)

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

/** Size tiers offered in the launcher, laddered by how many sub-agents run at
 *  once (CPU-bound, see cloud/sizes.py). Kept in sync with sizes.py's arm64
 *  lane; the numbers are display-only and match `SizeTier`. */
interface SizeTier {
  key: 'light' | 'balanced' | 'power' | 'light-x86' | 'balanced-x86' | 'power-x86'
  /** Which label/description to reuse — the x86 lane mirrors the arm64 shapes. */
  family: 'light' | 'balanced' | 'power'
  arch: 'arm64' | 'x86_64'
  instanceType: string
  vcpu: number
  ramGb: number
  diskGb: number
  subagents: number
  recommended?: boolean
}
const SIZE_TIERS: SizeTier[] = [
  { key: 'light', family: 'light', arch: 'arm64', instanceType: 't4g.xlarge', vcpu: 4, ramGb: 16, diskGb: 40, subagents: 3 },
  { key: 'balanced', family: 'balanced', arch: 'arm64', instanceType: 'm7g.2xlarge', vcpu: 8, ramGb: 32, diskGb: 60, subagents: 6, recommended: true },
  { key: 'power', family: 'power', arch: 'arm64', instanceType: 'm7g.4xlarge', vcpu: 16, ramGb: 64, diskGb: 80, subagents: 12 },
]
// The x86_64 lane, shown only when the disclosure is expanded. It exists because
// some images and toolchains are still amd64-only; the shapes mirror the arm64
// ladder so the sub-agent counts match. Keys match cloud/sizes.py's x86 lane.
const X86_TIERS: SizeTier[] = [
  { key: 'light-x86', family: 'light', arch: 'x86_64', instanceType: 't3.xlarge', vcpu: 4, ramGb: 16, diskGb: 40, subagents: 3 },
  { key: 'balanced-x86', family: 'balanced', arch: 'x86_64', instanceType: 'm7i.2xlarge', vcpu: 8, ramGb: 32, diskGb: 60, subagents: 6 },
  { key: 'power-x86', family: 'power', arch: 'x86_64', instanceType: 'm7i.4xlarge', vcpu: 16, ramGb: 64, diskGb: 80, subagents: 12 },
]

// Full literal keys per tier so the i18n key-reference gate can verify them
// statically (a map-field indirection is opaque to it).
const tierLabel = (key: SizeTier['family']) =>
  key === 'light'
    ? i18nT('pages.settings.remoteCrewPanel.tier_light')
    : key === 'balanced'
      ? i18nT('pages.settings.remoteCrewPanel.tier_development')
      : i18nT('pages.settings.remoteCrewPanel.tier_power')
const tierWhy = (key: SizeTier['family']) =>
  key === 'light'
    ? i18nT('pages.settings.remoteCrewPanel.tier_light_why')
    : key === 'balanced'
      ? i18nT('pages.settings.remoteCrewPanel.tier_development_why')
      : i18nT('pages.settings.remoteCrewPanel.tier_power_why')

const PRICING_CALCULATOR_URL = 'https://calculator.aws'
const PLUGIN_INSTALL_COMMAND = 'brew install --cask session-manager-plugin'

/** One selectable size card. Shared by the arm64 ladder and the x86 lane so the
 *  disclosure offers real choices rather than describing sizes it cannot select. */
function SizeCard({ tier, on, onPick }: { tier: SizeTier; on: boolean; onPick: (k: SizeTier['key']) => void }) {
  return (
    <button
      type="button"
      onClick={() => onPick(tier.key)}
      aria-pressed={on}
      aria-label={`${tierLabel(tier.family)} · ${tier.arch}`}
      className={`w-full text-left flex items-start gap-3 rounded-md border p-3.5 transition-all ${on ? 'border-accent bg-accent-subtle shadow-[0_0_0_3px_var(--accent-glow)]' : 'border-border-strong bg-bg-elevated hover:border-border-strong'}`}
    >
      <span className={`mt-0.5 w-4 h-4 shrink-0 rounded-full border-[1.5px] ${on ? 'border-accent bg-accent' : 'border-border-strong'}`} />
      <span className="min-w-0">
        <span className="font-bold text-[13px] text-text-strong flex items-center gap-2">
          {tierLabel(tier.family)}
          {tier.recommended && <Badge variant="aim">{i18nT('pages.settings.remoteCrewPanel.default_tag')}</Badge>}
          <span className="font-normal text-muted">· {i18nT('pages.settings.remoteCrewPanel.subagents', { n: tier.subagents })}</span>
        </span>
        <span className="block font-mono text-[12px] text-muted mt-1">
          {tier.instanceType} · {tier.arch} · {tier.vcpu} vCPU · {tier.ramGb} GB RAM · {tier.diskGb} GB disk
        </span>
        <span className="block text-[12px] text-text mt-1.5">{tierWhy(tier.family)}</span>
      </span>
    </button>
  )
}

/** One in-progress launch, shown among the crews as a "Setting up" row. */
function SettingUpRow({ job, onCancel, cancelling }: { job: LaunchJob; onCancel: (id: string) => void; cancelling: boolean }) {
  const total = job.steps.length || 4
  const current = Math.min(total, job.steps.filter(s => s.state === 'done').length + 1)
  const active = job.steps.find(s => s.state === 'active')
  return (
    <div className="flex items-start justify-between gap-3 py-2.5 border-b border-border last:border-b-0">
      <div className="flex items-start gap-3 min-w-0">
        <span className="mt-0.5 w-8 h-8 shrink-0 grid place-items-center rounded-md bg-accent-subtle text-accent">
          <Rocket size={16} />
        </span>
        <div className="min-w-0">
          <div className="text-text-strong text-sm font-medium flex items-center gap-2 flex-wrap">
            <span className="inline-block w-2 h-2 rounded-full bg-accent" aria-hidden />
            {i18nT('pages.settings.remoteCrewPanel.cloud_crew_name', { tag: job.tag })}
            <Badge variant="aim">{i18nT('pages.settings.remoteCrewPanel.setting_up')}</Badge>
          </div>
          <div className="text-[12px] text-muted mt-0.5">
            {job.region}
            {job.instance_id ? ` · ${job.instance_id}` : ''}
          </div>
          <div className="mt-1.5 rounded-md border border-accent-subtle bg-bg-elevated px-2.5 py-2">
            <div className="text-[12px] text-text-strong font-medium flex items-center gap-1.5">
              <RefreshCw size={12} className="animate-spin" />
              {i18nT('pages.settings.remoteCrewPanel.step_progress', { current, total })}
              {active?.label ? ` — ${active.label}` : ''}
            </div>
            <div className="text-[11px] text-muted mt-0.5">{i18nT('pages.settings.remoteCrewPanel.keeps_running')}</div>
          </div>
        </div>
      </div>
      <div className="shrink-0">
        <Btn onClick={() => onCancel(job.id)} disabled={cancelling} aria-label={i18nT('pages.settings.remoteCrewPanel.cancel_setup_of', { tag: job.tag })}>
          {cancelling ? i18nT('pages.settings.remoteCrewPanel.cancelling') : i18nT('pages.settings.remoteCrewPanel.cancel')}
        </Btn>
      </div>
    </div>
  )
}

/** One switchable crew — a cloud-launched instance (Stop / Delete by tag) or a
 *  hand-added machine (Remove). */
function CrewRow({
  inst,
  cloudTag,
  busy,
  confirmDelete,
  confirmRemove,
  onConnect,
  onDisconnect,
  onDiagnose,
  onRemove,
  onStop,
  onDelete,
  onRequestDelete,
  onRequestRemove,
}: {
  inst: InstanceView
  cloudTag: string | null
  busy: string
  confirmDelete: boolean
  confirmRemove: boolean
  onConnect: (id: string) => void
  onDisconnect: (id: string) => void
  onDiagnose: (id: string) => void
  onRemove: (id: string) => void
  onStop: (tag: string, coords: CloudCoords) => void
  onDelete: (tag: string, coords: CloudCoords) => void
  onRequestDelete: (tag: string | null) => void
  onRequestRemove: (id: string | null) => void
}) {
  const connected = inst.status.state === 'connected'
  const isCloud = cloudTag !== null
  // An SSM machine with no matching launch job is NOT necessarily hand-added: the CLI
  // launcher registers real cloud crews the same way, and those never produce a launch
  // job in this gateway's store. Calling them "added by you" and offering the plain
  // one-click Remove would unregister a live, billing instance and take away the only
  // place the dashboard could still delete it. We cannot prove which it is, so treat it
  // as possibly-cloud: same confirm step, and copy that says what Remove does and does
  // not do.
  const unverifiedCloud = !isCloud && inst.connection_method === 'ssm' && !!inst.ssm_target
  const target = inst.connection_method === 'ssm' ? inst.ssm_target : inst.ssh_host
  return (
    <div className="flex items-start justify-between gap-3 py-2.5 border-b border-border last:border-b-0">
      <div className="flex items-start gap-3 min-w-0">
        <span className={`mt-0.5 w-8 h-8 shrink-0 grid place-items-center rounded-md ${isCloud ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>
          {isCloud ? <Rocket size={16} /> : <Server size={16} />}
        </span>
        <div className="min-w-0">
          <div className="text-text-strong text-sm font-medium truncate">{inst.name}</div>
          <div className="text-[12px] text-muted truncate">
            <span className="uppercase tracking-wide text-muted-strong">{inst.connection_method === 'ssm' ? 'SSM' : 'SSH'}</span>{' '}
            {target}
            {inst.connection_method === 'ssm' && inst.aws_region ? ` (${inst.aws_region})` : ''} {i18nT('pages.settings.instancesPanel.port_2')} {inst.remote_port}
          </div>
          <div className="mt-1"><StatusBadge status={inst.status} /></div>
          <div className="text-[11px] text-muted-strong mt-1">
            {isCloud
              ? i18nT('pages.settings.remoteCrewPanel.launched_by_kiro_crew')
              : unverifiedCloud
                ? i18nT('pages.settings.remoteCrewPanel.unverified_cloud_note')
                : `${i18nT('pages.settings.remoteCrewPanel.added_by_you')} · ${i18nT('pages.settings.remoteCrewPanel.doesnt_manage')}`}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
        <Btn onClick={() => onDiagnose(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.diagnose_2', { name: inst.name })}>
          <Stethoscope className="lucide-inline" /> {busy === `diagnose:${inst.id}` ? '…' : i18nT('pages.settings.instancesPanel.diagnose')}
        </Btn>
        {connected ? (
          <Btn onClick={() => onDisconnect(inst.id)} disabled={!!busy}>
            <Unplug className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.disconnect')}
          </Btn>
        ) : (
          <Btn primary onClick={() => onConnect(inst.id)} disabled={!!busy}>
            <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? i18nT('pages.settings.instancesPanel.connecting') : i18nT('pages.settings.instancesPanel.connect')}
          </Btn>
        )}
        {isCloud ? (
          <>
            <Btn onClick={() => onStop(cloudTag, coordsOf(inst))} disabled={!!busy} aria-label={i18nT('pages.settings.remoteCrewPanel.stop_crew', { name: inst.name })}>
              {busy === `stop:${cloudTag}` ? '…' : i18nT('pages.settings.remoteCrewPanel.stop')}
            </Btn>
            {confirmDelete ? (
              <Btn danger onClick={() => onDelete(cloudTag, coordsOf(inst))} disabled={!!busy} aria-label={i18nT('pages.settings.remoteCrewPanel.confirm_delete_of', { name: inst.name })}>
                <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.confirm_delete')}
              </Btn>
            ) : (
              <Btn danger onClick={() => onRequestDelete(cloudTag)} disabled={!!busy} aria-label={i18nT('pages.settings.remoteCrewPanel.delete_crew', { name: inst.name })}>
                <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.delete')}
              </Btn>
            )}
          </>
        ) : unverifiedCloud ? (
          confirmRemove ? (
            <Btn danger onClick={() => onRemove(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
              <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.confirm_remove')}
            </Btn>
          ) : (
            <Btn danger onClick={() => onRequestRemove(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
              <Trash2 className="lucide-inline" />
            </Btn>
          )
        ) : (
          <Btn danger onClick={() => onRemove(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
            <Trash2 className="lucide-inline" />
          </Btn>
        )}
      </div>
    </div>
  )
}

/** One AWS prerequisite row (ok / warn) with optional command + actions. */
function PrereqRow({
  ok,
  title,
  detail,
  command,
  onCopyCommand,
  copied,
  onRecheck,
  extraAction,
}: {
  ok: boolean
  title: string
  detail: string
  command?: string
  onCopyCommand?: () => void
  copied?: boolean
  onRecheck?: () => void
  extraAction?: React.ReactNode
}) {
  return (
    <li className="flex items-start gap-3 py-2.5 border-b border-border last:border-b-0">
      <span className={`mt-0.5 w-[18px] h-[18px] shrink-0 grid place-items-center rounded-full ${ok ? 'bg-ok text-ok-fg' : 'bg-warn text-warn-fg'}`}>
        {ok ? <Check size={11} /> : <AlertTriangle size={11} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-text-strong">{title}</div>
        {detail ? <div className="text-[12px] text-muted mt-0.5 whitespace-pre-wrap">{detail}</div> : null}
        {command ? (
          <code className="block mt-1.5 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 font-mono text-[12px] text-accent overflow-x-auto">
            {command}
          </code>
        ) : null}
        {(onCopyCommand || onRecheck || extraAction) && (
          <div className="mt-2 flex gap-2 flex-wrap">
            {onCopyCommand && (
              <Btn onClick={onCopyCommand}>
                {copied ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_command')}
              </Btn>
            )}
            {extraAction}
            {onRecheck && (
              <Btn onClick={onRecheck}>
                <RefreshCw className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.re_check')}
              </Btn>
            )}
          </div>
        )}
      </div>
    </li>
  )
}

/** The launch-in-progress card (setup tab): 4 steps + device-code sign-in. */
function LaunchProgressCard({ job, onCancel, onSignin, cancelling }: {
  job: LaunchJob
  onCancel: (id: string) => void
  onSignin: (id: string) => void
  cancelling: boolean
}) {
  const terminal = isTerminal(job)
  const signin = job.signin ?? null
  return (
    <Card>
      <div className="flex items-center gap-2 mb-3">
        {job.status === 'done'
          ? <Badge variant="ok">{i18nT('pages.settings.instancesPanel.connect')}</Badge>
          : job.status === 'failed'
            ? <Badge variant="err">{i18nT('pages.settings.remoteCrewPanel.launch_failed_title')}</Badge>
            : <Badge variant="aim">{i18nT('pages.settings.remoteCrewPanel.launching')}</Badge>}
        <span className="text-text-strong text-sm font-medium">{i18nT('pages.settings.remoteCrewPanel.cloud_crew_name', { tag: job.tag })}</span>
        {!terminal && (
          <Btn className="ml-auto" onClick={() => onCancel(job.id)} disabled={cancelling} aria-label={i18nT('pages.settings.remoteCrewPanel.cancel_setup_of', { tag: job.tag })}>
            {cancelling ? i18nT('pages.settings.remoteCrewPanel.cancelling') : i18nT('pages.settings.remoteCrewPanel.cancel')}
          </Btn>
        )}
      </div>
      <ol className="m-0 p-0 list-none space-y-2">
        {job.steps.map(step => (
          <li key={step.key} className="flex items-start gap-2.5">
            <span className="mt-0.5 shrink-0">
              {step.state === 'done'
                ? <CheckCircle size={15} className="text-ok" />
                : step.state === 'failed'
                  ? <AlertTriangle size={15} className="text-danger" />
                  : step.state === 'active'
                    ? <Loader2 size={15} className="text-accent animate-spin" />
                    : <span className="inline-block w-[15px] h-[15px] rounded-full border border-border-strong" />}
            </span>
            <div className="min-w-0">
              <div className={`text-[13px] ${step.state === 'pending' ? 'text-muted' : 'text-text-strong'}`}>{step.label}</div>
              {step.detail ? <div className="text-[12px] text-muted mt-0.5 whitespace-pre-wrap">{step.detail}</div> : null}
            </div>
          </li>
        ))}
      </ol>

      {job.status === 'awaiting_signin' && (
        <div className="mt-3 rounded-md border border-accent-subtle bg-bg-elevated px-3 py-2.5">
          <div className="text-[13px] font-medium text-text-strong">{i18nT('pages.settings.remoteCrewPanel.sign_in_to_kiro')}</div>
          <div className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.remoteCrewPanel.sign_in_hint')}</div>
          {signin ? (
            <div className="mt-2 flex items-center gap-3 flex-wrap">
              <code className="rounded-md border border-border bg-bg px-2.5 py-1 font-mono text-[13px] text-accent">
                {i18nT('pages.settings.remoteCrewPanel.your_code', { code: signin.code })}
              </code>
              <a className="inline-flex items-center gap-1.5 text-accent text-[13px] font-medium hover:underline" href={signin.url} target="_blank" rel="noreferrer">
                <ExternalLink size={13} /> {i18nT('pages.settings.remoteCrewPanel.open_sign_in')}
              </a>
            </div>
          ) : (
            <div className="mt-2">
              <Btn onClick={() => onSignin(job.id)}>
                <ExternalLink className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.open_sign_in')}
              </Btn>
            </div>
          )}
        </div>
      )}

      {job.error ? <ErrorNotice message={job.error} className="mt-3" /> : null}
      <p className="mt-3 text-[12px] text-muted">
        {job.status === 'done' ? i18nT('pages.settings.remoteCrewPanel.launch_done') : i18nT('pages.settings.remoteCrewPanel.runs_on_gateway')}
      </p>
    </Card>
  )
}

export function RemoteCrewPanel() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const [tab, setTab] = useState<'crews' | 'setup'>('crews')

  // Setup-tab form + preflight state. `checkedProfile`/`checkedRegion` are the
  // committed values the preflight ran against, so typing a profile does not
  // hammer AWS on every keystroke — a check fires on first open, on blur, and on
  // the explicit Re-check.
  const [profile, setProfile] = useState('')
  const [region, setRegion] = useState('us-east-1')
  const [checkedProfile, setCheckedProfile] = useState('')
  const [checkedRegion, setCheckedRegion] = useState('us-east-1')
  const [showMoreSizes, setShowMoreSizes] = useState(false)
  const [sizeKey, setSizeKey] = useState<SizeTier['key']>('balanced')
  const [copied, setCopied] = useState<'command' | 'policy' | null>(null)
  const [activeLaunchId, setActiveLaunchId] = useState<string | null>(null)
  const [confirmDeleteTag, setConfirmDeleteTag] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [diagNote, setDiagNote] = useState<string | null>(null)
  const [restartPending, setRestartPending] = useState(false)

  const errMsg = useCallback(
    (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback),
    [],
  )

  const instancesQuery = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
  const disabled =
    instancesQuery.error instanceof ApiError &&
    instancesQuery.error.status === 403 &&
    /disabled/i.test(instancesQuery.error.message)
  // Any OTHER failure is a load error, not "you have no crews": rendering the
  // empty state over it would tell the user their crews are gone.
  // Both queries gate the crew list: a row's cloud-vs-manual identity comes from the
  // Enabled (data present, no 403) but not active => the flag was set after the
  // gateway started, so tunnels cannot be opened until it restarts. Connect would
  // return 503. Same distinction InstancesPanel draws.
  const needsRestart = !disabled && instancesQuery.data?.active === false

  const launchesQuery = useQuery({
    queryKey: ['cloud', 'launches'],
    queryFn: () => api.cloudLaunches(),
    // Owner-only; a non-owner 403 just yields no cloud rows.
    enabled: !disabled,
    refetchInterval: q => {
      const jobs = (q.state.data as { jobs?: LaunchJob[] } | undefined)?.jobs ?? []
      return jobs.some(isInProgress) ? 4000 : false
    },
  })

  const launches = useMemo(() => launchesQuery.data?.jobs ?? [], [launchesQuery.data])
  const inProgress = useMemo(() => launches.filter(isInProgress), [launches])

  // Both queries gate the crew list: a row's cloud-vs-manual identity comes from the
  // launch history, and the two destructive actions are NOT interchangeable. Treating
  // absent launch data as [] makes a real cloud crew render as "added by you", whose
  // trash button is a single unconfirmed click that unregisters the instance and
  // leaves the EC2 stack running and billing, invisible to the dashboard. So the list
  // waits until both are known, and surfaces either failure instead of guessing.
  const loadError = !disabled && (instancesQuery.isError || launchesQuery.isError)
  const listLoading = !disabled && (instancesQuery.isLoading || launchesQuery.isLoading)

  // `activeLaunchId` is component state, so navigating away and back loses it while
  // the gateway keeps driving the job. Falling back to the persisted in-progress job
  // is what makes "Keeps running if you leave this page" true for the sign-in step:
  // without it the device code and verification link — the only way to finish setup —
  // are unreachable after a remount, and the user's only recovery is cancel-and-relaunch.
  const effectiveLaunchId = activeLaunchId ?? inProgress[0]?.id ?? null

  const launchStatusQuery = useQuery({
    queryKey: ['cloud', 'launch', effectiveLaunchId],
    queryFn: () => api.cloudLaunchStatus(effectiveLaunchId as string),
    enabled: !!effectiveLaunchId,
    refetchInterval: q => {
      const s = (q.state.data as LaunchJob | undefined)?.status
      return s && IN_PROGRESS.includes(s) ? 3000 : false
    },
  })

  const preflightQuery = useQuery({
    queryKey: ['cloud', 'preflight', checkedProfile, checkedRegion],
    queryFn: () => api.cloudPreflight(checkedProfile || undefined, checkedRegion || undefined),
    enabled: tab === 'setup' && !disabled,
  })

  const instances = useMemo(() => instancesQuery.data?.instances ?? [], [instancesQuery.data])
  const warmCap = instancesQuery.data?.warm_set_cap || 5

  // instance_id → cloud tag, from every launch job that produced an instance.
  // An SSM instance whose target matches is a cloud crew, and this is its tag.
  const cloudTagByInstanceId = useMemo(() => {
    const m = new Map<string, string>()
    for (const j of launches) if (j.instance_id) m.set(j.instance_id, j.tag)
    return m
  }, [launches])

  const reloadInstances = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['instances'] })
  }, [queryClient])
  const reloadLaunches = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'launches'] })
  }, [queryClient])

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onMutate: () => { setActionErr(null); setDiagNote(null) },
    onSuccess: st => { if (st.state !== 'connected') setActionErr(st.error || i18nT('pages.settings.instancesPanel.connection_did_not_complete_try_diagnose_for_det')) },
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.connect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const disconnectMutation = useMutation({
    mutationFn: (id: string) => api.disconnectInstance(id),
    onMutate: () => setActionErr(null),
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.disconnect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const removeMutation = useMutation({
    mutationFn: async (id: string) => { await api.disconnectInstance(id).catch(() => {}); await api.removeInstance(id) },
    onMutate: () => setActionErr(null),
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.remove_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const diagnoseMutation = useMutation({
    mutationFn: (id: string) => api.instanceStatus(id, true),
    onMutate: () => { setActionErr(null); setDiagNote(null) },
    onSuccess: (st, id) => {
      const reason = st.diagnosis?.reason || st.error
      if (reason) setDiagNote(`${id}: ${reason}`)
    },
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.diagnose_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const cancelMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchCancel(id),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: reloadLaunches,
  })
  const stopMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudStop(v.tag, v.coords),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  const deleteMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudDestroy(v.tag, v.coords),
    onMutate: () => { setActionErr(null); setConfirmDeleteTag(null) },
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  const signinMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchSignin(id),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { if (activeLaunchId) void queryClient.invalidateQueries({ queryKey: ['cloud', 'launch', activeLaunchId] }) },
  })
  const launchMutation = useMutation({
    mutationFn: () => api.cloudLaunch({ profile, region, size_key: sizeKey }),
    onMutate: () => setActionErr(null),
    onSuccess: job => { setActiveLaunchId(job.id); reloadLaunches() },
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
  })
  const enableMutation = useMutation({
    mutationFn: () => api.patchConfig('instances.enabled', true),
    onSuccess: () => { setRestartPending(true); reloadInstances() },
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
  })

  const busy = connectMutation.isPending
    ? `connect:${connectMutation.variables}`
    : diagnoseMutation.isPending
      ? `diagnose:${diagnoseMutation.variables}`
      : stopMutation.isPending
        ? `stop:${stopMutation.variables}`
        : disconnectMutation.isPending || removeMutation.isPending || deleteMutation.isPending
          ? 'busy'
          : ''

  const runCheck = useCallback(() => {
    setCheckedProfile(profile)
    setCheckedRegion(region)
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'preflight'] })
  }, [profile, region, queryClient])

  const copyCommand = useCallback(() => {
    void copyToClipboard(PLUGIN_INSTALL_COMMAND)
    setCopied('command')
    setTimeout(() => setCopied(null), 1500)
  }, [])
  const copyPolicy = useCallback(async () => {
    try {
      const { policy } = await api.cloudIamPolicy()
      void copyToClipboard(policy)
      setCopied('policy')
      setTimeout(() => setCopied(null), 1500)
    } catch (e) {
      setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')))
    }
  }, [errMsg])

  const preflight: CloudPreflight | undefined = preflightQuery.data
  const blockingOk = !!preflight
    && preflight.reachable
    && !!preflight.account
    && preflight.ec2_reachable
    && preflight.cloudformation_reachable
    && preflight.ssm_reachable
    && preflight.session_manager_plugin

  // Prefer the polled detail, but fall back to the list's copy so the card (and its
  // device code) is present on the very first render after a remount, before the
  // status query has resolved.
  const activeJob = launchStatusQuery.data ?? inProgress[0] ?? null

  // A launch that reaches `done` has just added a crew, but nothing else invalidates
  // the instances cache and switching tabs does not remount this component — so "Your
  // crews" would keep showing the pre-launch list until an unrelated refetch happened.
  // Keyed by job id so this fires once per launch instead of on every poll.
  const reconciledLaunch = useRef<string | null>(null)
  useEffect(() => {
    if (!activeJob || !isTerminal(activeJob)) return
    if (reconciledLaunch.current === activeJob.id) return
    reconciledLaunch.current = activeJob.id
    reloadInstances()
  }, [activeJob, reloadInstances])

  // ── Disabled feature gate (mirrors InstancesPanel) ──
  if (disabled) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-text font-medium mb-1">
          <Server className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.multi_instance_management_is_off')}
        </div>
        <p className="text-[13px] text-muted mb-3">{i18nT('pages.settings.instancesPanel.enable_it_to_let_this_gateway_open_ssh_tunnels_t')}</p>
        {restartPending && (
          <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warn/10 text-warn border border-warn/30">
            <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
            <span>{i18nT('pages.settings.instancesPanel.disabled_in_config_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>) {i18nT('pages.settings.instancesPanel.to_fully_tear_down_any_tunnels_still_running_fro')}</span>
          </div>
        )}
        <Btn primary onClick={() => enableMutation.mutate()} disabled={enableMutation.isPending}>
          <Power className="lucide-inline" /> {enableMutation.isPending ? i18nT('pages.settings.instancesPanel.enabling') : i18nT('pages.settings.instancesPanel.enable_remote_crew_management')}
        </Btn>
        <ErrorNotice message={actionErr} className="mt-2" />
      </Card>
    )
  }

  const Tabs = (
    <div className="flex gap-1 border-b border-border mb-5">
      <button
        type="button"
        onClick={() => setTab('crews')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.your_crews')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'crews' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        {i18nT('pages.settings.remoteCrewPanel.your_crews')}
        <span className={`text-[11px] px-1.5 rounded-full ${tab === 'crews' ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>{instances.length}</span>
      </button>
      <button
        type="button"
        onClick={() => setTab('setup')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.set_up_a_new_one')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'setup' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        <Rocket size={14} /> {i18nT('pages.settings.remoteCrewPanel.set_up_a_new_one')}
        {inProgress.length > 0 && <span className="w-1.5 h-1.5 rounded-full bg-accent" aria-hidden />}
      </button>
    </div>
  )

  const Notices = (
    <>
      {actionErr && <ErrorNotice message={actionErr} onDismiss={() => setActionErr(null)} className="mb-3" />}
      {diagNote && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-accent/10 text-accent border border-accent/30">
          <Stethoscope size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{diagNote}</span>
          <button type="button" aria-label={i18nT('pages.settings.instancesPanel.dismiss_diagnosis')} className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setDiagNote(null)}><X size={12} /></button>
        </div>
      )}
    </>
  )

  return (
    <div>
      {Tabs}
      {Notices}

      {tab === 'crews' ? (
        <div className="space-y-4">
          <Card>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2 text-text font-medium">
                <Server className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.crews_you_can_switch_to')}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.up_to_warm', { n: warmCap })}</span>
                <Btn onClick={reloadInstances} aria-label={i18nT('pages.settings.instancesPanel.refresh')}><RefreshCw className="lucide-inline" /></Btn>
              </div>
            </div>
            {needsRestart && (
              <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warn/10 text-warn border border-warn/30">
                <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
                <span>{i18nT('pages.settings.instancesPanel.disabled_in_config_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>) {i18nT('pages.settings.instancesPanel.to_fully_tear_down_any_tunnels_still_running_fro')}</span>
              </div>
            )}
            {listLoading ? (
              <div className="flex items-center gap-2 text-muted text-sm py-2">
                <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.instancesPanel.loading')}
              </div>
            ) : loadError ? (
              // Never fall through to the empty state on a failed load — that
              // reads as "your crews are gone" when the list simply did not load.
              <div className="py-1">
                <ErrorNotice message={errMsg(instancesQuery.error ?? launchesQuery.error, i18nT('pages.settings.instancesPanel.unknown_error'))} />
                <Btn className="mt-2" onClick={() => { reloadInstances(); reloadLaunches() }}>
                  <RefreshCw className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.refresh')}
                </Btn>
              </div>
            ) : (
              <div>
                {inProgress.map(job => (
                  <SettingUpRow key={job.id} job={job} cancelling={cancelMutation.isPending && cancelMutation.variables === job.id} onCancel={id => cancelMutation.mutate(id)} />
                ))}
                {instances.map(inst => (
                  <CrewRow
                    key={inst.id}
                    inst={inst}
                    cloudTag={inst.connection_method === 'ssm' && inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) ?? null : null}
                    busy={busy}
                    confirmDelete={confirmDeleteTag !== null && confirmDeleteTag === (inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) : null)}
                    confirmRemove={confirmRemoveId === inst.id}
                    onConnect={id => connectMutation.mutate(id)}
                    onDisconnect={id => disconnectMutation.mutate(id)}
                    onDiagnose={id => diagnoseMutation.mutate(id)}
                    onRemove={id => removeMutation.mutate(id)}
                    onStop={(tag, coords) => stopMutation.mutate({ tag, coords })}
                    onDelete={(tag, coords) => deleteMutation.mutate({ tag, coords })}
                    onRequestDelete={tag => setConfirmDeleteTag(tag)}
                    onRequestRemove={id => setConfirmRemoveId(id)}
                  />
                ))}
                {inProgress.length === 0 && instances.length === 0 && (
                  <div className="text-[13px] text-muted py-1">{i18nT('pages.settings.remoteCrewPanel.no_crews')}</div>
                )}
              </div>
            )}
            {confirmDeleteTag !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.delete_warning')}</p>}
            {confirmRemoveId !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.remove_warning')}</p>}
          </Card>

          <AddInstanceForm onAdded={reloadInstances} usedPorts={instances.map(i => i.remote_port)} />
        </div>
      ) : (
        <div className="space-y-4">
          {/* AWS prerequisites */}
          <Card>
            <div className="flex items-center gap-2 mb-3 text-text font-medium">
              <CheckCircle className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.before_you_start')}
            </div>
            {preflightQuery.isLoading ? (
              <div className="flex items-center gap-2 text-muted text-sm py-2">
                <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.checking')}
              </div>
            ) : preflight ? (
              <ul className="m-0 p-0 list-none">
                <PrereqRow
                  ok={preflight.reachable && !!preflight.account}
                  title={i18nT('pages.settings.remoteCrewPanel.prereq_credentials')}
                  detail={preflight.reachable && preflight.account
                    ? i18nT('pages.settings.remoteCrewPanel.credentials_ok', { profile: checkedProfile || i18nT('pages.settings.remoteCrewPanel.default_profile'), account: preflight.account })
                    : (preflight.detail || i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
                  onRecheck={runCheck}
                />
                <PrereqRow ok={preflight.ec2_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ec2')} detail={preflight.ec2_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
                <PrereqRow ok={preflight.cloudformation_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_cloudformation')} detail={preflight.cloudformation_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} extraAction={preflight.cloudformation_reachable ? undefined : <Btn onClick={copyPolicy}>{copied === 'policy' ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied === 'policy' ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_policy_json')}</Btn>} />
                <PrereqRow ok={preflight.ssm_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ssm')} detail={preflight.ssm_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
                <PrereqRow
                  ok={preflight.session_manager_plugin}
                  title={i18nT('pages.settings.remoteCrewPanel.plugin')}
                  detail={preflight.session_manager_plugin ? i18nT('pages.settings.remoteCrewPanel.plugin_ok') : i18nT('pages.settings.remoteCrewPanel.plugin_missing')}
                  command={preflight.session_manager_plugin ? undefined : PLUGIN_INSTALL_COMMAND}
                  onCopyCommand={preflight.session_manager_plugin ? undefined : copyCommand}
                  copied={copied === 'command'}
                  onRecheck={preflight.session_manager_plugin ? undefined : runCheck}
                />
              </ul>
            ) : (
              <div className="text-[13px] text-muted py-1">
                {preflightQuery.error ? errMsg(preflightQuery.error, i18nT('pages.settings.remoteCrewPanel.credentials_bad')) : i18nT('pages.settings.remoteCrewPanel.credentials_bad')}
                <div className="mt-2"><Btn onClick={runCheck}><RefreshCw className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.re_check')}</Btn></div>
              </div>
            )}
            <p className="mt-3 text-[12px] text-muted flex items-start gap-1.5">
              <CheckCircle size={13} className="mt-0.5 shrink-0 text-ok" /> {i18nT('pages.settings.remoteCrewPanel.profile_name_only')}
            </p>
          </Card>

          {/* Launch form */}
          <Card>
            <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.new_cloud_crew')}</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label htmlFor="cloud-profile" className="flex flex-col gap-1 text-[13px] text-muted">
                {i18nT('pages.settings.instancesPanel.aws_profile')}
                <input
                  id="cloud-profile"
                  aria-label={i18nT('pages.settings.instancesPanel.aws_profile')}
                  className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring"
                  value={profile}
                  onChange={e => setProfile(e.target.value)}
                  onBlur={runCheck}
                  placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')}
                />
              </label>
              <label htmlFor="cloud-region" className="flex flex-col gap-1 text-[13px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.region')}
                <input
                  id="cloud-region"
                  aria-label={i18nT('pages.settings.remoteCrewPanel.region')}
                  className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring"
                  value={region}
                  onChange={e => setRegion(e.target.value)}
                  onBlur={runCheck}
                  placeholder="us-east-1"
                />
              </label>
            </div>

            <div className="mt-4">
              <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.size')}</div>
              <div className="space-y-2.5">
                {SIZE_TIERS.map(tier => (
                  <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
                ))}
              </div>

              <button
                type="button"
                onClick={() => setShowMoreSizes(v => !v)}
                className="mt-2.5 w-full flex items-center gap-2 text-[12px] text-muted px-3 py-2 rounded-md border border-dashed border-border-strong hover:text-text"
              >
                <ChevronDown size={14} className={`transition-transform ${showMoreSizes ? 'rotate-180' : ''}`} /> {i18nT('pages.settings.remoteCrewPanel.more_sizes')}
              </button>
              {showMoreSizes && (
                <>
                  <p className="mt-2 text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.more_sizes_hint')}</p>
                  <div className="mt-2 space-y-2.5">
                    {X86_TIERS.map(tier => (
                      <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
                    ))}
                  </div>
                </>
              )}
            </div>

            <div className="mt-4 flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warn" />
              <div className="text-[12px] text-text">
                {i18nT('pages.settings.remoteCrewPanel.billing')}{' '}
                <a className="text-accent font-medium hover:underline inline-flex items-center gap-1" href={PRICING_CALCULATOR_URL} target="_blank" rel="noreferrer">
                  {i18nT('pages.settings.remoteCrewPanel.pricing_calculator')} <ExternalLink size={12} />
                </a>
              </div>
            </div>

            <div className="mt-4 flex items-center gap-3 flex-wrap">
              <Btn primary onClick={() => launchMutation.mutate()} disabled={!blockingOk || launchMutation.isPending}>
                <Rocket className="lucide-inline" /> {launchMutation.isPending ? i18nT('pages.settings.remoteCrewPanel.launching') : i18nT('pages.settings.remoteCrewPanel.launch')}
              </Btn>
              <span className="text-[12px] text-muted">{blockingOk ? i18nT('pages.settings.remoteCrewPanel.ready_in_6') : i18nT('pages.settings.remoteCrewPanel.finish_prereqs')}</span>
            </div>
          </Card>

          {activeJob && (
            <LaunchProgressCard
              job={activeJob}
              cancelling={cancelMutation.isPending && cancelMutation.variables === activeJob.id}
              onCancel={id => cancelMutation.mutate(id)}
              onSignin={id => signinMutation.mutate(id)}
            />
          )}
        </div>
      )}
    </div>
  )
}
