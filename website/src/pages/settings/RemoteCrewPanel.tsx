/**
 * RemoteCrewPanel — Settings → Remote Crew. One page, two tabs, one per KIND of
 * thing this panel lists:
 *
 *   1. "Remote crews" (default) — GATEWAYS. A whole machine serving a whole
 *      roster, with a dashboard you switch to from the top header. Cloud-launched
 *      crews are told apart from hand-added ones by correlating each SSM
 *      instance's target id with a launch job's `instance_id`, so cloud rows can
 *      offer the cloud lifecycle (Stop / Delete-by-tag) that a plain tunnel row
 *      cannot. Its footer button reveals the EC2 launcher: an AWS prerequisite
 *      checklist (from the cloud preflight) and a form that spins up a gateway on
 *      the user's OWN AWS account. That launcher is a long form, so it opens on
 *      request rather than sitting above the answer to "which crews do I have".
 *      The add-a-machine form lives here too.
 *   2. "Remote mates" — ONE AGENT each. A Fargate task carrying one member's
 *      bundle, with no dashboard and no roster, so it is something to chat to
 *      rather than to switch to. Its footer button opens the mate picker
 *      (`DeployMateDialog`), and the lane it deploys into states which mate its
 *      image serves before a launch rather than after a refused one.
 *
 * Neither tab offers the other's lane: `crewLanes` drops the Fargate row and the
 * picker drops the built-in EC2 row, because a gateway installer cannot answer
 * "which mate?" and a single-agent task is not a place a gateway can run.
 *
 * The launch progress card sits at PANEL level, outside both tabs: a launch can be
 * started from either, and it is the only surface that reports a failed one.
 *
 * The instance CRUD (connect / disconnect / diagnose / remove) and the
 * enable/disable gate mirror InstancesPanel; the add-existing form and the
 * StatusBadge are reused from it directly.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
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
  Circle,
  Copy,
  Check,
  ExternalLink,
  ChevronDown,
  Power,
  Loader2,
  MoreHorizontal,
  Pencil,
  Play,
  X,
  KeyRound,
} from 'lucide-react'
import {
  api,
  ApiError,
  isAuthExpiredError,
  type InstanceView,
  type LaunchJob,
  type CloudPreflight,
  type CloudCoords,
  type RemoteProvisioner,
} from '../../api/client'
import {
  BUILTIN_PROVISIONER_ID,
  FARGATE_PROVISIONER_ID,
  WARM_SET_CAP_AUTO_CEILING,
  hasDashboardPane,
  launchIsInFlight,
  usesSsmTransport,
  PRICING_CALCULATOR_URL,
} from '../../utils/remoteCrew'
import { Card, Btn, Badge, IconButton } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import {
  announceChainedCrew,
  clearChainRefusal,
  readChainRefusal,
  subscribeChainRefusal,
} from '../../lib/chainAnnounce'
import {
  BUILTIN_REMOTE_PROVISIONER_KINDS,
  canRenderRemoteProvisionerKind,
  getRemoteProvisionerRenderer,
} from '../../components/remoteProvisionerRenderers'
import type { ErrorReport } from '../../utils/errorReport'
import { parseErrorCode } from '../../utils/errorReport'
import { reportInstanceFailure } from '../../utils/instanceFailureReport'
import { readPersistedString, usePersistedString } from '../../hooks/usePersistedString'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { AUTO_CONNECT_KEY } from '../../hooks/useAutoConnectInstances'
import { copyToClipboard } from '../../utils/clipboard'
import { useAppDispatch, useAppSelector } from '../../store'
import { removeWarm, setCrewEditForm } from '../../store/instancesSlice'
import { i18nT } from '../../i18n/t'
import { AddInstanceForm } from './InstancesPanel'
import {
  EditInstanceForm,
  instanceFormFromView,
  type InstanceDraft,
} from './InstanceFormFields'
import CrewAvatar from '../../components/CrewAvatar'
import DeployMateDialog, { type DeployMateRequest } from './DeployMateDialog'
import {
  captionWords,
  detailNote,
  detailRows,
  isMate,
  laneHasLiveReading,
  laneLabel,
  laneOf,
  readClock,
  statusDotClass,
  statusWords,
} from './remoteLane'


/** A launch job the user is still waiting on (not yet a switchable crew). The
 *  status list is the shared one in utils/remoteCrew, so the Members page's
 *  cloud panel and this one classify a launch the same way. */
const isInProgress = (j: LaunchJob) => launchIsInFlight(j.status)

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
function TurnUrlField({ url, mateName }: { url: string; mateName: string }) {
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const label = i18nT('pages.settings.remoteCrewPanel.copy_turn_url', { name: mateName })
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
        <code className="flex-1 min-w-0 font-mono text-[12px] overflow-x-auto whitespace-nowrap scrollbar-none text-card-fg">
          {url}
        </code>
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

/** Remembered across navigation — see the state declarations for why. */
const CLOUD_PROFILE_KEY = 'mc-cloud-profile'
const CLOUD_REGION_KEY = 'mc-cloud-region'
// The launch form's third input. Persisted like the two above rather than held in
// the component, because every way out of this panel unmounts it — the agent
// hand-off's navigation, a sidebar click, the back button — and a size picked and
// then silently reset to the recommended default is a launch the user did not ask
// for.
const CLOUD_SIZE_KEY = 'mc-cloud-size'
// Which provisioner the setup tab is drawing. Persisted for the same reason as
// the three fields above — every way out of this panel unmounts it, and silently
// reverting to the built-in EC2 launcher would put the user in front of a
// different form (and a different bill) than the one they chose.
const CLOUD_PROVISIONER_KEY = 'mc-cloud-provisioner'
const DEFAULT_REGION = 'us-east-1'

/** A launch that has reached a final state — nothing more will happen to it. */
const TERMINAL: LaunchJob['status'][] = ['done', 'failed', 'cancelled']
const isTerminal = (j: LaunchJob) => TERMINAL.includes(j.status)

/** The connect (register) step ran — the crew exists under Your crews, so a
 *  sign-in can be re-run against it rather than provisioning anything. */
const isRegistered = (j: LaunchJob) => j.steps.some(st => st.key === 'connect' && st.state === 'done')

/** A launch that created a crew but whose Kiro sign-in never confirmed. Such a
 *  crew is registered (Stop / Delete work) but a chat on it fails with "not
 *  logged in", so it must not read as ready to connect. */
const needsSignin = (j: LaunchJob) =>
  isTerminal(j) && !!j.instance_id && j.signin_detected !== true && isRegistered(j)

/** Whether the Connect step's icon should read as waiting rather than done.
 *  What it answers is "is this crew signed in", NOT "is this job over" — a
 *  registered crew waiting on a code is unsigned for the whole approval, which
 *  is most of the time a user spends looking at the card. */
const connectWaiting = (j: LaunchJob) => isRegistered(j) && j.signin_detected !== true

/** The launch chose a company (IAM Identity Center) identity, so its code is
 *  approved through the organization's portal and not the Builder ID one.
 *  A job from an older gateway carries no `login_target` at all, which means
 *  Builder ID — it must not be told it has a company account it never had. */
const isSsoLaunch = (j: LaunchJob) => !!j.login_target?.start_url

/** What Cancel actually destroys, which is not one thing.
 *
 *  On a registered crew the click only stops the Kiro sign-in — the instance and
 *  the crew row survive, and the job goes back to done. On a launch that has not
 *  registered yet it removes the instance being created. One word for both left
 *  the reader unable to tell which, so the label names the blast radius. The
 *  accessible name carries it too: an aria-label that still said "Cancel setup"
 *  would take the distinction back away from the readers who need it most. */
function cancelCopy(job: LaunchJob) {
  return isRegistered(job)
    ? {
      label: i18nT('pages.settings.remoteCrewPanel.cancel_sign_in'),
      aria: i18nT('pages.settings.remoteCrewPanel.cancel_sign_in_of', { tag: job.tag }),
      // Reading the label was the ONLY way to tell the two apart, so the reader
      // had to read carefully every time to avoid deleting the machine. The
      // destructive one is the danger button; this one is not.
      danger: false,
    }
    : {
      label: i18nT('pages.settings.remoteCrewPanel.cancel_remove_instance'),
      aria: i18nT('pages.settings.remoteCrewPanel.cancel_setup_remove_of', { tag: job.tag }),
      danger: true,
    }
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

// NOTE: the session-manager-plugin install command is NOT hardcoded here. It has to
// match the platform of the machine running the gateway — which may be a Linux host
// while this dashboard is open on a Mac — so the preflight response carries it.

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
          {i18nT('pages.settings.remoteCrewPanel.tier_spec', {
            instanceType: tier.instanceType,
            arch: tier.arch,
            vcpu: tier.vcpu,
            ramGb: tier.ramGb,
            diskGb: tier.diskGb,
          })}
        </span>
        <span className="block text-[12px] text-text mt-1.5">{tierWhy(tier.family)}</span>
      </span>
    </button>
  )
}

/** One selectable provisioner. Shown only when the gateway offers more than one
 *  the frontend can draw, so the stock build (EC2 alone) renders no selector at
 *  all. The label is server-authored and rendered verbatim, like a step label —
 *  the core has no catalog key for a provisioner it does not know about. */
function ProvisionerCard({ provisioner, on, onPick }: {
  provisioner: RemoteProvisioner
  on: boolean
  onPick: (id: string) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(provisioner.id)}
      aria-pressed={on}
      aria-label={provisioner.label}
      className={`w-full text-left flex items-start gap-3 rounded-md border p-3.5 transition-all ${on ? 'border-accent bg-accent-subtle shadow-[0_0_0_3px_var(--accent-glow)]' : 'border-border-strong bg-bg-elevated hover:border-border-strong'}`}
    >
      <span className={`mt-0.5 w-4 h-4 shrink-0 rounded-full border-[1.5px] ${on ? 'border-accent bg-accent' : 'border-border-strong'}`} />
      <span className="min-w-0 font-bold text-[13px] text-text-strong">{provisioner.label}</span>
    </button>
  )
}

/** A gateway failure in words a reader can act on.
 *
 *  `errMsg` yields whatever the transport said — "Failed to fetch", "504 Gateway
 *  Timeout", "NetworkError when attempting to fetch resource" — and pasting that
 *  into a sentence tells the reader nothing they can do. A recognised shape is
 *  replaced by a WHOLE sentence of its own (never a fragment dropped into another
 *  string: that is untranslatable). Anything unrecognised returns null, and the
 *  caller falls back to the message that carries the raw detail — a wrong guess
 *  is worse than the transport's own words.
 */
/** Whether the failure means the GATEWAY itself did not answer.
 *
 *  The agent chat is served by the same gateway, so a hand-off in this state
 *  offers a chat that cannot load. Same predicate as the unreachable sentence, so
 *  the copy and the hand-off decision can never disagree.
 */
function gatewayIsDown(detail: string): boolean {
  return /failed to fetch|networkerror|network error|load failed|err_connection|unreachable|econnrefused/.test(
    detail.toLowerCase(),
  )
}

function failureSentence(detail: string): string | null {
  const d = detail.toLowerCase()
  if (gatewayIsDown(d)) {
    return i18nT('pages.settings.remoteCrewPanel.failure_unreachable')
  }
  // Status codes and timeout wording only. A bare "gateway" would swallow every
  // message that merely NAMES the gateway ("disk quota exceeded on the gateway"),
  // which is the case this function must pass through untouched.
  if (/\b(502|503|504)\b|bad gateway|gateway time-?out|timeout|timed out/.test(d)) {
    return i18nT('pages.settings.remoteCrewPanel.failure_busy')
  }
  if (/\b(401|403)\b|unauthor|forbidden|expired/.test(d)) {
    return i18nT('pages.settings.remoteCrewPanel.failure_signed_out')
  }
  return null
}

/** What the last sign-in fetch/recheck for a job came back with, when it did not
 *  come back with a code. `not_signed_in_yet` is the gateway's 409
 *  `no_signin_pending` on a preserved code: the box was re-probed and the
 *  approval has not landed. `request_failed` is any other failure. */
type SigninNotice =
  | { kind: 'not_signed_in_yet' }
  | { kind: 'request_failed'; detail: string }

/** The device code the user must approve, with its actions. Shared by the setup
 *  card and the crew row so the code is reachable from BOTH tabs — a user who
 *  left the setup tab must not have to find their way back to finish. */
function SigninPromptBlock({ job, onRestart, restarting, onFetch, fetching, notice, compact, mateName }: {
  job: LaunchJob
  onRestart: (id: string) => void
  restarting: boolean
  /** Ask the gateway for the pending prompt — the only way to see a code for a
   *  job that reached `awaiting_signin` before this tab was open. */
  onFetch: (id: string) => void
  fetching: boolean
  /** The outcome of the last `onFetch` for THIS job, rendered beside the button
   *  that produced it. A recheck that finds the approval not landed yet used to
   *  re-render the identical screen, so the reader could not tell the check had
   *  run; a failed fetch used to surface only as the panel-level banner, far
   *  from the button it belongs to. */
  notice?: SigninNotice | null
  compact?: boolean
  /** The crew this block belongs to, when nothing directly above the block names
   *  it. In `Your crews` the block sits among several rows, and an unnamed
   *  "This crew" there attributes the sign-in to whichever row the reader
   *  happened to be looking at. The setup card names the crew in its own header,
   *  so it passes nothing and keeps the shorter title. */
  mateName?: string
}) {
  // The code is a one-time value the reader must type into a browser, and the
  // reader tried CLICKING it to copy. `copyToClipboard` is the guarded helper,
  // not `navigator.clipboard` directly: on a plain-HTTP remote dashboard the API
  // is undefined and a direct call throws synchronously, so the copy has to be
  // able to report failure rather than paint success regardless.
  const [codeCopied, setCodeCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const copyCode = useCallback(async (code: string) => {
    setCopyFailed(false)
    let ok = false
    try {
      ok = await copyToClipboard(code)
    } catch {
      ok = false
    }
    if (!ok) {
      setCopyFailed(true)
      return
    }
    setCodeCopied(true)
    window.setTimeout(() => setCodeCopied(false), 1500)
  }, [])
  const awaiting = job.status === 'awaiting_signin'
  const starting = job.status === 'running' && job.steps.some(st => st.key === 'signin' && st.state === 'active')
  const signin = job.signin ?? null
  // A code the gateway is still polling for is live: offer only the page. A code
  // left over from a wait that ran out MAY still work, so keep it, but the way
  // forward when it does not is a fresh one — never a second button that
  // silently restarts the login while the shown code is being typed in.
  const stale = !awaiting && !!signin
  // The gateway already holds the prompt for a job awaiting sign-in; making the
  // reader click to reveal it left them unable to tell what decided shown vs
  // hidden. Ask once on mount. The button stays as the retry for a fetch that
  // failed, and for the stale-code recheck.
  const autoFetched = useRef(false)
  useEffect(() => {
    if (awaiting && !signin && !fetching && !autoFetched.current) {
      autoFetched.current = true
      onFetch(job.id)
    }
  }, [awaiting, signin, fetching, onFetch, job.id])
  // The restart route acts on a crew: it refuses a job that never created one.
  // Offering the button there would answer a click with a 400.
  const canRestart = isRegistered(job) && !!job.instance_id
  return (
    <div className={`${compact ? 'mt-2' : 'mt-3'} rounded-md border border-accent-subtle bg-bg-elevated px-3 py-2.5`} data-testid="signin-prompt">
      {/* Not "Sign in to Kiro" again: that string is already the step, the badge
          and the banner, so four copies left the reader unsure which one to act
          on. This box's job is the code.

          And it names WHICH sign-in. "Sign-in" means two things in this flow —
          the Kiro sign-in that is the step, and the company SSO sign-in that
          carries it out — and nothing on screen said the second was how the
          first happens; the reader had to assume it. */}
      {/* Before any code exists the title must not say "approve the code" --
          the reader has not been given one. Name the action instead. */}
      <div className="text-[13px] font-medium text-text-strong">
        {!signin
          // Two no-code states, two titles. Awaiting: the gateway already holds
          // a code and the button SHOWS it -- "Get a sign-in code" over a button
          // that says it starts nothing read as a contradiction. Terminal with no
          // code: nothing exists yet, so "Get" is the truthful verb.
          ? awaiting
            ? mateName
              ? i18nT('pages.settings.remoteCrewPanel.code_ready_for', { name: mateName })
              : i18nT('pages.settings.remoteCrewPanel.code_ready')
            : mateName
              ? i18nT('pages.settings.remoteCrewPanel.get_a_code_for', { name: mateName })
              : i18nT('pages.settings.remoteCrewPanel.get_a_code')
          : mateName
            ? isSsoLaunch(job)
              ? i18nT('pages.settings.remoteCrewPanel.approve_your_code_sso_for', { name: mateName })
              : i18nT('pages.settings.remoteCrewPanel.approve_your_code_for', { name: mateName })
            : isSsoLaunch(job)
              ? i18nT('pages.settings.remoteCrewPanel.approve_your_code_sso')
              : i18nT('pages.settings.remoteCrewPanel.approve_your_code')}
      </div>
      <div className="text-[12px] text-muted mt-0.5">
        {starting
          ? i18nT('pages.settings.remoteCrewPanel.sign_in_starting')
          : stale
            ? i18nT('pages.settings.remoteCrewPanel.sign_in_unconfirmed')
            : awaiting
              // Names the two sign-ins as ONE act. The reader could not tell
              // whether the Kiro sign-in step and the company SSO approval were
              // one login or two, and had to assume the second carried out the
              // first.
              ? isSsoLaunch(job)
                ? i18nT('pages.settings.remoteCrewPanel.sign_in_hint_sso')
                : i18nT('pages.settings.remoteCrewPanel.sign_in_hint')
              : i18nT('pages.settings.remoteCrewPanel.sign_in_needed')}
      </div>
      {starting ? (
        <div className="mt-2 text-[12px] text-muted inline-flex items-center gap-1.5">
          {/* In the CARD the badge above already says a code is being fetched, so
              repeating it here was the same fact twice on one screen; this line
              adds what the badge does not say -- how long to wait. In a crew row
              (`compact`) there is no badge, so the line carries the fact. */}
          <Loader2 size={13} className="text-accent animate-spin" />{' '}
          {compact
            ? i18nT('pages.settings.remoteCrewPanel.sign_in_preparing_code')
            : i18nT('pages.settings.remoteCrewPanel.sign_in_preparing_wait')}
        </div>
      ) : (
        <>
          {/* Two rows, not one. The code chip and its page link are the CODE's
              actions; the recheck / fetch / start primary is the JOB's. Together
              they made three peer actions in one row on a stale code, which is
              what left the reader ranking them. */}
          {signin && (
          <div className="mt-2 flex items-center gap-3 flex-wrap" data-testid="signin-code-row">
              <button
                type="button"
                onClick={() => copyCode(signin.code)}
                aria-label={i18nT('pages.settings.remoteCrewPanel.copy_code_aria', { code: signin.code })}
                title={i18nT('pages.settings.remoteCrewPanel.copy_code_aria', { code: signin.code })}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 py-1 font-mono text-[13px] text-accent hover:border-accent-subtle focus-ring"
                data-testid="signin-code-copy"
              >
                {i18nT('pages.settings.remoteCrewPanel.your_code', { code: signin.code })}
                {codeCopied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
                {/* The WORD, not only the icon. Every recovery goes through this
                    chip, and a reader who has to guess from a glyph that it copies
                    is a reader who may not try it. */}
                <span className="font-body text-[12px] text-muted">
                  {codeCopied
                    ? i18nT('pages.settings.remoteCrewPanel.copied')
                    : i18nT('pages.settings.remoteCrewPanel.copy_word')}
                </span>
              </button>
              {/* Visible, not sr-only: the icon swap alone is easy to miss, and a
                  copy that FAILED must say so — the reader would otherwise paste
                  nothing into the browser and blame the code. */}
              <a className="inline-flex items-center gap-1.5 text-accent text-[13px] font-medium hover:underline" href={signin.url} target="_blank" rel="noreferrer">
                <ExternalLink size={13} /> {i18nT('pages.settings.remoteCrewPanel.open_sign_in')}
              </a>
          </div>
          )}
          {/* Under the row, not inside it: inserted between the chip and the page
              link, this notice pushed the link sideways at the moment the reader
              was going for it. */}
          {signin && copyFailed && (
                // The panel's own surface, the one `PrereqRow` uses for its copy
                // failures -- not a bare span. A failure styled unlike every
                // neighbouring error is the one the reader skips, and `askAgent`
                // is what makes it actionable.
                /* No hand-off: this notice sits beside the one-time device code the
                   reader is copying by hand into another window. The hand-off
                   navigates to chat and unmounts the chip that shows the code,
                   mid-copy; the remedy ("select the text and copy it manually") is
                   complete on its own, and an agent cannot supply a clipboard the
                   browser refused. A third control here would also push the code
                   row past two actions. */
            <ErrorNotice
              variant="inline"
              className="mt-1.5"
              message={i18nT('pages.settings.remoteCrewPanel.copy_failed')}
              testId="signin-copy-error"
            />
          )}
          {(stale || (awaiting && !signin) || (!awaiting && canRestart && !signin)) && (
          <div className="mt-2 flex items-center gap-3 flex-wrap" data-testid="signin-action-row">
          {(stale || (awaiting && !signin)) && (
            // Two states, one call. `awaiting && !signin`: the prompt lives on the
            // gateway and a job already awaiting sign-in when this tab opened has
            // nothing to render until the dashboard asks. `stale`: the job is over
            // but its code was preserved, and the gateway re-probes the box on this
            // same call -- so a user who approved that code in their browser gets
            // the crew marked signed in.
            //
            // Without this the re-probe was unreachable: it lives behind `onFetch`,
            // which only rendered while `awaiting`. The reader's only option on a
            // preserved code was to replace it, discarding the approval they had
            // just given.
            //
            // It is NOT the external link, and must not wear its icon or its label:
            // this click asks the gateway for the pending prompt and renders the
            // code inline — it opens nothing. "Open sign-in page" under an
            // external-link icon promised a browser tab, so the reader waited for a
            // tab that never came. The real link is the anchor beside the code,
            // which appears once this click has produced one.
            <Btn primary onClick={() => onFetch(job.id)} disabled={fetching}>
                            {stale
                ? i18nT('pages.settings.remoteCrewPanel.recheck_sign_in')
                : i18nT('pages.settings.remoteCrewPanel.fetch_sign_in_code')}
            </Btn>
          )}
          {!awaiting && canRestart && !signin && (
            // The only sign-in there is when no code exists, so it is the primary.
            // With a code on screen this action moves OUT of the row entirely —
            // see the hint below: the row there already holds the code chip, the
            // code's own page link and the recheck primary, and a fourth sibling
            // button left the reader choosing between four peers with no ranking.
            <Btn primary onClick={() => onRestart(job.id)} disabled={restarting}>
                            {restarting
                ? i18nT('pages.settings.remoteCrewPanel.sign_in_starting_short')
                : i18nT('pages.settings.remoteCrewPanel.start_sign_in')}
            </Btn>
          )}
          {notice?.kind === 'not_signed_in_yet' && !fetching && (
            // The recheck RAN and the approval had not landed: the same screen
            // again is not an answer. Says so, in the block's own tone -- it is
            // the ordinary outcome of clicking early, not an error.
            <span role="status" className="text-[12px] text-muted" data-testid="signin-recheck-result">
              {i18nT('pages.settings.remoteCrewPanel.recheck_not_yet')}
            </span>
          )}
          {notice?.kind === 'request_failed' && !fetching && (
            // Hand-off ON for a failure the agent can look into, and nothing here
            // is lost by leaving: the job, its steps and any preserved code are
            // persisted and re-render on return. This is the action row, so the
            // extra control does not join the code chip and its link.
            //
            // No hand-off: when the failure is that the gateway did not answer at
            // all, the agent chat is served by that same gateway -- the button
            // would open a chat that cannot load, from the one screen that just
            // told the reader the gateway is down. The message carries the whole
            // remedy there (check that it is running, then try again).
            <ErrorNotice
              variant="inline"
              className="mt-0"
              message={failureSentence(notice.detail)
                ?? (stale
                  ? i18nT('pages.settings.remoteCrewPanel.recheck_failed', { error: notice.detail })
                  : i18nT('pages.settings.remoteCrewPanel.fetch_code_failed', { error: notice.detail }))}
              askAgent={!gatewayIsDown(notice.detail)}
              testId="signin-fetch-error"
            />
          )}
          </div>
          )}
        </>
      )}
      {/* What the click PRODUCES. The two recovery buttons read correctly only if
          you already know whether they resume the code on screen or replace it,
          and replacing is irreversible for a code being typed elsewhere. Gated on
          the same condition as the button: a hint with no button beside it
          describes a click the reader cannot make. */}
      {/* Every recovery button says what the click produces -- including the
          fetch button, which was the only one whose hint was gated off, because
          the gate required `!awaiting`. That is what left "Start sign-in" and
          "Show the sign-in code" looking like they might do the same thing. */}
      {!starting && (awaiting ? !signin : canRestart) && (
        <div className="mt-1.5 text-[12px] text-muted" data-testid="signin-recovery-hint">
          {awaiting
            ? i18nT('pages.settings.remoteCrewPanel.fetch_code_hint')
            : signin
              ? i18nT('pages.settings.remoteCrewPanel.recheck_hint')
              : i18nT('pages.settings.remoteCrewPanel.start_sign_in_hint')}
          {/* The replacement, as an inline text link inside the sentence that
              already warns what it costs — not a fourth button in the row above.
              It is still a real <button> with the same `onRestart` handler and
              the same label, so it keeps its name, its keyboard reachability and
              its disabled state while a restart is in flight; only its rank
              changed, from a peer of the primary to the sentence's own link. */}
          {!awaiting && canRestart && signin && (
            <>
              {' '}
              <button
                type="button"
                onClick={() => onRestart(job.id)}
                disabled={restarting}
                className="text-[12px] text-accent hover:underline bg-transparent border-none cursor-pointer p-0 font-body disabled:opacity-30 disabled:cursor-not-allowed focus-ring"
                data-testid="signin-get-new-code"
              >
                {restarting
                  ? i18nT('pages.settings.remoteCrewPanel.sign_in_starting_short')
                  : i18nT('pages.settings.remoteCrewPanel.start_over_new_code')}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** One in-progress launch, shown among the crews as a "Setting up" row. */
/** The launch/sign-in cancel, in both places a launch is shown.
 *
 *  Armed, then fired -- the same two steps Delete uses on a crew row -- but ONLY
 *  for the half that destroys something. This click deletes the instance being
 *  created and there is no undo; a single unguarded button is what the reader
 *  "would not dare" press, because nothing on screen said whether it asks first.
 *  Stopping a sign-in keeps the instance and the crew row, so a confirm there
 *  would be ceremony, and ceremony everywhere is what makes a real warning
 *  invisible.
 */
function CancelLaunchBtn({ job, onCancel, cancelling, className }: {
  job: LaunchJob
  onCancel: (id: string) => void
  cancelling: boolean
  className?: string
}) {
  const [armed, setArmed] = useState(false)
  const copy = cancelCopy(job)
  if (!copy.danger) {
    return (
      <Btn className={className} onClick={() => onCancel(job.id)} disabled={cancelling} aria-label={copy.aria} title={i18nT('pages.settings.remoteCrewPanel.cancel_sign_in_hint')}>
        {cancelling ? i18nT('pages.settings.remoteCrewPanel.cancelling') : copy.label}
      </Btn>
    )
  }
  if (!armed) {
    return (
      <Btn className={className} danger onClick={() => setArmed(true)} disabled={cancelling} aria-label={copy.aria}>
        {cancelling ? i18nT('pages.settings.remoteCrewPanel.cancelling') : copy.label}
      </Btn>
    )
  }
  return (
    <>
      {/* No aria-label override: the visible label already names the instance it
          removes, and an aria-label would REPLACE that name for a screen reader
          with the pre-arm wording -- so the confirm would announce itself as the
          button the reader already pressed. */}
      <Btn className={className} danger onClick={() => onCancel(job.id)} disabled={cancelling}>
        {cancelling
          ? i18nT('pages.settings.remoteCrewPanel.cancelling')
          : i18nT('pages.settings.remoteCrewPanel.confirm_cancel_remove', { tag: job.tag })}
      </Btn>
      {/* An armed destructive button needs a way out, as on the crew row. */}
      <Btn onClick={() => setArmed(false)} disabled={cancelling}>
        {i18nT('pages.settings.remoteCrewPanel.keep_setting_up')}
      </Btn>
      <p className="text-[12px] text-warn basis-full m-0" data-testid="cancel-remove-warning">
        {i18nT('pages.settings.remoteCrewPanel.cancel_remove_warning')}
      </p>
    </>
  )
}

function SettingUpRow({ job }: { job: LaunchJob }) {
  const total = job.steps.length || 4
  const current = Math.min(total, job.steps.filter(s => s.state === 'done').length + 1)
  const active = job.steps.find(s => s.state === 'active')
  return (
    // Stacked below `sm`, side by side above it. The cancel label names its blast
    // radius ("Cancel and remove the crew"), and that long string in a
    // `shrink-0` slot left the crew name and the step line one word per line on a
    // phone -- for the whole provisioning wait. Wrapping the label instead would
    // keep the squeeze; the button gets its own line.
    <div className="flex flex-col sm:flex-row items-stretch sm:items-start sm:justify-between gap-2 sm:gap-3 py-2.5 border-b border-border last:border-b-0">
      <div className="flex items-start gap-3 min-w-0">
        {/* The crew being deployed, wearing its own face from the first second — the
            same avatar the finished card carries, so the row does not change identity
            when the launch lands. A job from before `mate_name` existed named no crew
            and falls back to the launch tag it always showed. */}
        <CrewAvatar
          seed={job.mate_name || job.tag}
          size={32}
          className="mt-0.5 shrink-0 rounded-md border border-border bg-bg-elevated"
        />
        <div className="min-w-0">
          <div className="text-text-strong text-sm font-medium flex items-center gap-2 flex-wrap">
            {job.mate_name || i18nT('pages.settings.remoteCrewPanel.cloud_crew_name', { tag: job.tag })}
            <Badge variant="aim">{i18nT('pages.settings.remoteCrewPanel.setting_up')}</Badge>
          </div>
          {/* Plain words and a step count. The region and the half-created instance id
              that used to sit here are identifiers for a machine that may not exist
              yet; the launch's own progress card on the other tab carries them, and
              this row's question is only how far along it is. */}
          <div className="mt-1 flex items-center gap-1.5 flex-wrap text-[13px] text-muted">
            <RefreshCw size={12} className="animate-spin shrink-0 text-warn" aria-hidden />
            <span>{i18nT('pages.settings.remoteCrewPanel.step_progress', { current, total })}</span>
            {active?.label ? <span>— {active.label}</span> : null}
          </div>
          <div className="text-[11px] text-muted-strong mt-1">
            {i18nT('pages.settings.remoteCrewPanel.keeps_running')}
          </div>
          {/* One segment per step, so how far along it is reads without being read. */}
          <div className="mt-2 flex gap-1.5 max-w-[58ch]" aria-hidden>
            {job.steps.map(st => (
              <span
                key={st.key}
                className={`h-[3px] flex-1 rounded-full ${
                  st.state === 'done'
                    ? 'bg-ok'
                    : st.state === 'failed'
                      ? 'bg-danger'
                      : st.state === 'active'
                        ? 'bg-warn'
                        : 'bg-border-strong'
                }`}
              />
            ))}
          </div>
        </div>
      </div>
      {/* No Cancel here. `CancelLaunchBtn` is the launch's own control and it sits on the
          progress card, which is at panel level and so is on screen whenever this row
          is: two buttons carrying the same accessible name, for the same one action, is
          a choice the reader has to make and cannot. This row's job is to hold the
          arriving crew's place in the list it will join. */}
    </div>
  )
}

/** One switchable crew — a cloud-launched instance (Stop / Delete by tag) or a
 *  hand-added machine (Remove). */
function CrewRow({
  inst,
  cloudTag,
  busy,
  deleting,
  confirmDelete,
  confirmRemove,
  onConnect,
  onDisconnect,
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
  readAt,
  onCheckAgain,
  avatarSeed,
}: {
  inst: InstanceView
  cloudTag: string | null
  /**
   * What a MATE row's ghost face is drawn from: the mate a launch deployed here, or ''
   * to fall back to the row's own name.
   *
   * Not the row name, because that carries the launch tag so two tasks of one mate can
   * be told apart -- and a face that followed the tag would give one agent a different
   * picture per launch, which is the opposite of what the avatar is for.
   */
  avatarSeed: string
  /**
   * `HH:MM` of the moment this row's status was last read, or '' when unknown.
   *
   * It is the instances query's own last success, which is exactly when this state
   * was read — not a timestamp the backend stamps on the record. Printed only for a
   * lane whose status reflects something outside this process (see
   * `laneHasLiveReading`), because for an SSH tunnel the age is not information.
   */
  readAt: string
  /** Re-read the list. What "Check again" does; the panel owns the invalidation. */
  onCheckAgain: () => void
  busy: string
  deleting: boolean
  confirmDelete: boolean
  confirmRemove: boolean
  onConnect: (id: string) => void
  onDisconnect: (id: string) => void
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
}) {
  const connected = inst.status.state === 'connected'
  const isCloud = cloudTag !== null
  // An unsigned crew still connects: the user can sign in on the crew itself once
  // connected. The badge and the sign-in controls stay so the missing sign-in is
  // visible, but they never hold Connect back.
  //
  // NOT gated on `!connected`: auto-connect is default-on, so an unsigned crew is
  // routinely connected already — and that is precisely when the badge and the
  // recovery controls are needed, because the chats are the thing that fails.
  const awaitingSignin = signinJob !== null
  // A stop/start this row asked for is still in flight.
  const lifecycleBusy = busy === `stop:${cloudTag}` || busy === `start:${cloudTag}`
  // States that occupy the row's second control slot with an inline button.
  const transient =
    deleting || lifecycleBusy || (isCloud && confirmDelete) || (!isCloud && confirmRemove)
  // A fargate crew has no dashboard; while its forward is up, the card shows
  // the turn URL the status carries instead of offering something to open.
  const turnUrl = inst.connection_method === 'fargate' && connected ? inst.status?.turn_url || '' : ''
  // The lane, in the user's vocabulary: which of SSH / EC2 / Fargate / Coder this crew
  // runs in. Derived once, because the chip, the status wording and the Details rows
  // all branch on it and three separate derivations could disagree.
  const lane = laneOf(inst)
  // A mate (one agent) or a crew (a gateway). It decides the avatar, and nothing else on
  // this row: both get the same lane chip, the same plain-words status and the same
  // Details, because those answer the same questions for either thing.
  const mate = isMate(inst)
  return (
    <div className="py-2.5 border-b border-border last:border-b-0" data-crew-id={inst.id}>
    <div className="flex items-start justify-between gap-3">
      <div className="flex items-start gap-3 min-w-0">
        {/* A MATE wears its own face; a CREW wears a machine.

            The avatar is an identity, and only one of these two has one: a mate IS an
            agent, drawn with the same component and the same seed the Members page uses,
            so one agent is one picture everywhere. A crew is a gateway serving a whole
            roster -- giving it one member's face would claim it is that member, and
            giving it a generic ghost would invent an identity it does not have. So it
            keeps a machine glyph, and the lane chip beside it says which kind. */}
        {mate ? (
          <CrewAvatar
            seed={avatarSeed || inst.name}
            size={32}
            className="mt-0.5 shrink-0 rounded-md border border-border bg-bg-elevated"
          />
        ) : (
          <span
            className={`mt-0.5 w-8 h-8 shrink-0 grid place-items-center rounded-md ${isCloud ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}
          >
            {isCloud ? <Rocket size={16} /> : <Server size={16} />}
          </span>
        )}
        <div className="min-w-0">
          <div className="text-text-strong text-sm font-medium truncate">{inst.name}</div>
          {/* ONE chip. The host, port, ECS target and instance id that used to share
              this line are in the kebab's Details: a reader scanning this list is
              asking which crew and whether they can use it, and an identifier
              answers neither — while the line was long enough to need truncating in
              exactly the tail that tells two tasks apart. */}
          <div className="mt-0.5 flex items-center gap-1.5 flex-wrap">
            <Badge variant="muted" title={connectionTypeHint(inst)}>
              {laneLabel(lane)}
            </Badge>
            {awaitingSignin && (
              <Badge variant="warn" title={i18nT('pages.settings.remoteCrewPanel.needs_sign_in_hint')}>
                <KeyRound className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.needs_sign_in')}
              </Badge>
            )}
          </div>
          {/* Plain words, then the moment they were read, then a way to re-read.
              The time and the button appear only for a lane whose answer comes from
              AWS: an SSH tunnel is this process's own socket, so printing an age for
              it would invent one, and a refresh beside it would promise news that
              only a reconnect can produce. */}
          <div className="mt-1 flex items-center gap-1.5 flex-wrap text-[13px] text-muted">
            <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${statusDotClass(inst)}`} aria-hidden />
            <span>{statusWords(inst)}</span>
            {laneHasLiveReading(inst) && readAt && (
              <>
                <span className="text-border-strong" aria-hidden>·</span>
                <span className="font-mono text-[12px]">
                  {i18nT('pages.settings.remoteCrewPanel.as_of', { time: readAt })}
                </span>
                <button
                  type="button"
                  onClick={onCheckAgain}
                  className="text-accent text-[12px] hover:underline"
                >
                  {i18nT('pages.settings.remoteCrewPanel.check_again')}
                </button>
              </>
            )}
            {/* The tunnel's own error from the backend, where it always was. */}
            <ErrorNotice variant="inline" className="max-w-[240px]" message={inst.status.error} askAgent />
          </div>
          <div className="text-[11px] text-muted-strong mt-1 truncate max-w-[52ch]">
            {captionWords(inst, isCloud)}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">
        {/* A row shows at most two controls. While a transient state occupies
            them — an armed confirm plus its Cancel, or a teardown in progress —
            the primary action stands down; connecting is not what the user is
            being asked about at that moment. */}
        {transient ? null : connected ? (
          <Btn onClick={() => onDisconnect(inst.id)} disabled={!!busy || deleting}>
            <Unplug className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.disconnect')}
          </Btn>
        ) : (
          <Btn primary onClick={() => onConnect(inst.id)} disabled={!!busy || deleting}>
            <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? i18nT('pages.settings.instancesPanel.connecting') : i18nT('pages.settings.instancesPanel.connect')}
          </Btn>
        )}
        {/* A teardown and a pending confirmation stay OUT of the overflow menu:
            both are transient states the user must see without reopening a menu —
            the delete only requested the teardown, and AWS confirms minutes later
            when the row is dropped. Hiding that read as "nothing happened". */}
        {deleting ? (
          <Btn danger disabled aria-label={i18nT('pages.settings.remoteCrewPanel.deleting')}>
            <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.deleting')}
          </Btn>
        ) : lifecycleBusy ? (
          // The action was chosen from the menu, which then closed. Report its
          // progress on the row under the SAME accessible name the menu item
          // carried, so the crew a request belongs to is never ambiguous.
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
            <Btn danger onClick={() => onDelete(cloudTag, coordsOf(inst))} disabled={!!busy} aria-label={i18nT('pages.settings.remoteCrewPanel.confirm_delete_of', { name: inst.name })}>
              {/* Names its target on screen, not only to assistive tech: this click
                  terminates an EC2 instance, and "Confirm delete" beside two other
                  rows does not say WHICH. */}
              <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.delete_crew', { name: inst.name })}
            </Btn>
            {/* An armed destructive button needs a way out. The overflow menu is
                hidden while armed, so without this a mis-click leaves the row
                showing nothing but a button that terminates an EC2 instance. */}
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
        {/* A row shows at most two controls. Connect/Disconnect is the primary
            action and everything else lives in this menu; while a transient
            action occupies the second slot the menu yields, since it is
            disabled in those states anyway. */}
        {!transient && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <IconButton
              aria-label={i18nT('pages.settings.remoteCrewPanel.more_actions', { name: inst.name })}
              disabled={!!busy || deleting}
            >
              <MoreHorizontal className="lucide-inline" />
            </IconButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[260px]">
            {/* Details FIRST, and not a menu item: the identifiers are what the card
                gave up, so the menu that took them has to hand them back without a
                further click. A definition list rather than a sentence, because a
                reader here is comparing a value to one they hold — that is a lookup.
                `onSelect` prevented so copying a value does not close the menu. */}
            <div
              className="px-2 pt-1.5 pb-2"
              onSelect={e => e.preventDefault()}
              data-testid={`crew-details-${inst.id}`}
            >
              <div className="text-[10.5px] uppercase tracking-[.08em] text-muted mb-1">
                {i18nT('pages.settings.remoteCrewPanel.details')}
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-2.5 gap-y-1 m-0 items-baseline">
                {detailRows(inst).map(row => (
                  <div key={row.label} className="contents">
                    <dt className="text-[10.5px] text-muted whitespace-nowrap">{row.label}</dt>
                    <dd className="m-0 font-mono text-[11px] text-text break-all">{row.value}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-2 mb-0 text-[11px] text-muted-strong">
                {detailNote(inst, isCloud)}
              </p>
            </div>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="gap-2 text-[13px]"
              onSelect={() => onDiagnose(inst.id)}
              aria-label={i18nT('pages.settings.instancesPanel.diagnose_2', { name: inst.name })}
            >
              <Stethoscope className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.diagnose')}
            </DropdownMenuItem>
            <DropdownMenuItem className="gap-2 text-[13px]" onSelect={() => onEdit(inst.id)}>
              <Pencil className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.edit_settings')}
            </DropdownMenuItem>
            {isCloud ? (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="gap-2 text-[13px]"
                  onSelect={() => onStop(cloudTag, coordsOf(inst))}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.stop_crew', { name: inst.name })}
                >
                  <Power className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.stop')}
                </DropdownMenuItem>
                {/* Stop without Start is a one-way door: the route exists and the client
                    method existed, but nothing called it — a stopped crew had no path back
                    to running from the dashboard, while its EBS volume kept billing. */}
                <DropdownMenuItem
                  className="gap-2 text-[13px]"
                  onSelect={() => onStart(cloudTag, coordsOf(inst))}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.start_crew', { name: inst.name })}
                >
                  <Play className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.start')}
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="gap-2 text-[13px] text-danger"
                  onSelect={() => onRequestDelete(cloudTag)}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.delete_crew', { name: inst.name })}
                >
                  <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.delete')}
                </DropdownMenuItem>
              </>
            ) : (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="gap-2 text-[13px] text-danger"
                  // Always confirm-gated: the label ends in an ellipsis because a
                  // second step follows, and the record being removed (host, port,
                  // TTL, profile) is the one this panel exists to let you correct
                  // — losing it to a single click has no undo.
                  onSelect={() => onRequestRemove(inst.id)}
                  aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}
                >
                  <Trash2 className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.remove')}
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        )}
      </div>
    </div>
    {turnUrl && <TurnUrlField url={turnUrl} mateName={inst.name} />}
    {/* BELOW the row header, and naming its crew. Rendered above the name it read
        as a page-level warning banner about the whole panel, and with several rows
        it attributed the sign-in to whichever crew the reader was looking at. */}
    {awaitingSignin && signinJob && (
      <SigninPromptBlock
        job={signinJob}
        mateName={inst.name}
        onRestart={onRestartSignin}
        restarting={restartingSignin}
        onFetch={onFetchSignin}
        fetching={fetchingSignin}
        notice={signinNotice}
        compact
      />
    )}
    {blocked && (
      // At the row, and assertive: the menu closes on select, so a refusal that
      // renders anywhere else reads as the click having done nothing at all.
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

/**
 * The built-in form for the `aws_fargate` lane.
 *
 * Deliberately not a form. A Fargate launch takes no input this panel could collect:
 * the cluster, the subnets, the security groups, the digest-pinned image and the crew's
 * secret references all come from the operator's `cloud.json`, the engine refuses to
 * guess any of them, and the size is one of the same three interactive tiers the EC2
 * ladder names (the engine maps each to a cpu/memory pair). So the only decision left
 * is WHICH CREW — and that is the picker's question, which is why this card's one
 * control opens it.
 *
 * What the card does do is say what the lane is before a launch rather than after one:
 * the crew its configured image serves, and the region the launch will name. A lane
 * that answers "which crew does this deploy" only by refusing a launch is a lane the
 * operator cannot plan around.
 */
/**
 * What the mate lane IS, for a reader deciding whether to deploy into it.
 *
 * No action of its own: the mates tab's own footer carries "Deploy a mate to the
 * cloud" one card above this, and a second identical button with the same footnote
 * under it read as two different launches.
 */
function FargateLaunchCard({
  provisioner,
  region,
  sizeKey,
}: {
  provisioner: RemoteProvisioner
  region: string
  sizeKey: string
}) {
  const serves = provisioner.serves_mate ?? ''
  return (
    <Card>
      <div className="text-text font-medium mb-1">{provisioner.label}</div>
      <p className="text-[13px] text-muted mt-0 mb-3">
        {i18nT('pages.settings.remoteCrewPanel.fargate_lane_intro')}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 m-0 items-baseline">
        {serves && (
          <div className="contents">
            <dt className="text-[11px] uppercase tracking-[.08em] text-muted whitespace-nowrap">
              {i18nT('pages.settings.remoteCrewPanel.fargate_serves_mate')}
            </dt>
            <dd className="m-0 text-[13px] text-text-strong font-medium">{serves}</dd>
          </div>
        )}
        <div className="contents">
          <dt className="text-[11px] uppercase tracking-[.08em] text-muted whitespace-nowrap">
            {i18nT('pages.settings.remoteCrewPanel.launches_into')}
          </dt>
          <dd className="m-0 text-[13px] text-text">{region}</dd>
        </div>
        <div className="contents">
          <dt className="text-[11px] uppercase tracking-[.08em] text-muted whitespace-nowrap">
            {i18nT('pages.settings.remoteCrewPanel.size')}
          </dt>
          <dd className="m-0 text-[13px] text-text">{sizeKey}</dd>
        </div>
      </dl>
      {/* Where the two values above come from. They are the panel's AWS settings, which
          live in the crews tab's launcher -- and the region is not decoration: every ECS
          call this launch makes is made in it, so a reader who cannot tell where it was
          set cannot tell that it is wrong. */}
      <p className="mt-3 mb-0 text-[11px] text-muted-strong">
        {i18nT('pages.settings.remoteCrewPanel.lane_settings_from')}
      </p>
    </Card>
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
  rechecking,
  extraAction,
  error,
}: {
  ok: boolean
  title: string
  detail: string
  command?: string
  onCopyCommand?: () => void
  copied?: boolean
  onRecheck?: () => void
  rechecking?: boolean
  extraAction?: React.ReactNode
  /** A failure of one of this row's own actions, rendered beside the buttons
   *  that caused it rather than in the page-level notices above the fold. */
  error?: string
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
          <div className="mt-2 flex gap-2 flex-wrap items-center">
            {onCopyCommand && (
              <Btn onClick={onCopyCommand}>
                {copied ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_command')}
              </Btn>
            )}
            {extraAction}
            {onRecheck && (
              // The re-check refetches an already-populated query, so the card's
              // `isLoading` spinner never fires (isLoading is pending-AND-fetching, and
              // pending is false once data exists). Without a busy state here, clicking
              // Re-check on an unchanged profile looks like nothing happened at all —
              // the probe shells out to the AWS CLI for a second or more, then paints an
              // identical result.
              <Btn onClick={onRecheck} disabled={!!rechecking}>
                <RefreshCw className={`lucide-inline${rechecking ? ' animate-spin' : ''}`} />{' '}
                {rechecking
                  ? i18nT('pages.settings.remoteCrewPanel.checking')
                  : i18nT('pages.settings.remoteCrewPanel.re_check')}
              </Btn>
            )}
          </div>
        )}
        {/* Its own line under the action row, not inside it: the notice carries
            the hand-off button, and Copy + Re-check already fill the row's
            two-action budget. askAgent ON: the launch form on this tab persists
            its size and account, so the navigation loses nothing. */}
        <ErrorNotice variant="inline" className="mt-1.5" message={error} askAgent />
      </div>
    </li>
  )
}

/** The launch-in-progress card (setup tab): 4 steps + device-code sign-in. */
function LaunchProgressCard({
  job, onCancel, onRestartSignin, restartingSignin, onFetchSignin, fetchingSignin, signinNotice, cancelling,
}: {
  job: LaunchJob
  onCancel: (id: string) => void
  onRestartSignin: (id: string) => void
  restartingSignin: boolean
  onFetchSignin: (id: string) => void
  fetchingSignin: boolean
  signinNotice: SigninNotice | null
  cancelling: boolean
}) {
  const terminal = isTerminal(job)
  // The gateway deliberately KEEPS job.signin when the sign-in wait ran out (it is
  // cleared only once sign-in is confirmed, or when a restart reaps the job), so the
  // user can still finish from the dashboard. Gating the block on `awaiting_signin`
  // alone hid the code the moment the job went terminal — making that promise a dead
  // end. A crew that exists but never confirmed its sign-in — with or without a
  // surviving code — is the unconfirmed case, and the block offers a fresh code.
  const unsigned = needsSignin(job)
  const signinInFlight = !terminal && isRegistered(job)
  const waiting = connectWaiting(job)
  // A surviving code stays visible on ANY terminal job, `needsSignin` or not: the
  // gateway keeps it precisely so setup can be finished here, and a job whose
  // steps do not name the connect step would otherwise lose it.
  const showSignin = job.status === 'awaiting_signin' || unsigned || signinInFlight || (terminal && !!job.signin)
  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap mb-3">
        {job.status === 'done'
          ? unsigned
            ? <Badge variant="warn" title={i18nT('pages.settings.remoteCrewPanel.needs_sign_in_hint')}><KeyRound className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.needs_sign_in')}</Badge>
            : <Badge variant="ok">{i18nT('pages.settings.instancesPanel.connect')}</Badge>
          : job.status === 'failed'
            ? <Badge variant="err">{i18nT('pages.settings.remoteCrewPanel.launch_failed_title')}</Badge>
            // "Launching…" under a card whose whole body is asking the reader to
            // approve a code says the machine is busy and there is nothing to do
            // — so the reader waited for a launch that was in fact waiting for
            // THEM. `awaiting_signin` is a blocked-on-you state, not progress:
            // same warn key icon the terminal unsigned badge uses, so the two
            // read as one condition seen at two moments.
            : (job.status === 'awaiting_signin' && !job.signin && fetchingSignin) || (signinInFlight && job.status !== 'awaiting_signin')
              // "Getting your sign-in code..." only while a fetch or a restart is
              // actually in flight. An idle no-code prompt beside an enabled
              // "Show the sign-in code" button must not say wait while the button
              // says act; that state falls through to the waiting badge below.
              ? <Badge variant="aim"><Loader2 className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.sign_in_preparing_code')}</Badge>
            : job.status === 'awaiting_signin'
              // Also the restart route: a running job whose connect step is already
              // done is signing in, not launching -- the create step is ticked, so
              // "Launching..." above it left the reader asking what was still launching.
              ? <Badge variant="warn"><KeyRound className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.awaiting_sign_in')}</Badge>
              // Same string as the row's pill (`SettingUpRow`). One in-progress
              // job wearing "Setting up" in the list and "Launching…" on its
              // card read as two states the reader had to reconcile.
              : <Badge variant="aim">{i18nT('pages.settings.remoteCrewPanel.setting_up')}</Badge>}
        <span className="text-text-strong text-sm font-medium">{i18nT('pages.settings.remoteCrewPanel.cloud_crew_name', { tag: job.tag })}</span>
        {/* A sign-in retry and a launch both show this button, and they destroy
            very different things — the reader "would not dare click it" without
            knowing which. Split on the same `isRegistered` test the rest of the
            card uses. */}
        {!terminal && (
          <CancelLaunchBtn className="ml-auto" job={job} onCancel={onCancel} cancelling={cancelling} />
        )}
      </div>
      <ol className="m-0 p-0 list-none space-y-2">
        {job.steps.map(step => (
          <li key={step.key} className="flex items-start gap-2.5">
            <span className="mt-0.5 shrink-0">
              {/* The connect step of an unsigned launch DID run — the crew is
                  registered — but a green check beside "sign in to Kiro on the
                  crew" reads as done and not-done at the same time.
                  Mark it as the waiting state its own detail describes.

                  The sign-in step of that same crew is the SAME waiting state. An
                  empty circle beside a step the card is actively asking you to
                  finish reads as going backwards from "in progress"; `skipped` is
                  what a launch that registered without confirming leaves there. */}
              {/* Two waiting states, two marks. The sign-in step is in the user's
                  court (key). The connect step DID run and is only held until the
                  sign-in lands -- a warn-tinted hollow circle, so the reader does
                  not see the same key on a status and a step and read them as
                  one thing. */}
              {waiting && step.key === 'signin' && (step.state === 'skipped' || step.state === 'pending')
                ? <KeyRound size={15} className="text-warn" data-testid="step-waiting-signin" />
                : waiting && step.key === 'connect' && step.state === 'done'
                ? <Circle size={15} className="text-warn" data-testid="step-waiting-connect" aria-hidden="true" />
                : step.state === 'done'
                ? <CheckCircle size={15} className="text-ok" />
                : step.state === 'failed'
                  ? <AlertTriangle size={15} className="text-danger" />
                  : step.state === 'active'
                    ? <Loader2 size={15} className="text-accent animate-spin" />
                    : <span className="inline-block w-[15px] h-[15px] rounded-full border border-border-strong" />}
            </span>
            <div className="min-w-0">
              <div className={`text-[13px] ${step.state === 'pending' ? 'text-muted' : 'text-text-strong'}`}>{step.label}</div>
              {/* Progress detail, not the error surface: `launch_job.py` sets
                  `job.error` on every path that marks a step failed (the reaper
                  and the exception handler, which also copies the same text into
                  `detail`), and `job.error` renders through the ErrorNotice below.
                  Painting it red here too would show one failure twice. */}
              {step.detail ? <div className="text-[12px] text-muted mt-0.5 whitespace-pre-wrap">{step.detail}</div> : null}
            </div>
          </li>
        ))}
      </ol>

      {showSignin && (
        <SigninPromptBlock
          job={job}
          onRestart={onRestartSignin}
          restarting={restartingSignin}
          onFetch={onFetchSignin}
          fetching={fetchingSignin}
          notice={signinNotice}
        />
      )}

      {/* No hand-off: this card sits in the setup flow whose form fields
          (name, host, size) are still live — navigating away discards them. */}
      {job.error ? <ErrorNotice message={job.error} className="mt-3" /> : null}
      <p className="mt-3 text-[12px] text-muted">
        {job.status === 'done'
          ? unsigned
            // "your new instance is ready" under a Needs sign-in badge is the
            // exact claim this whole card exists to stop making.
            ? i18nT('pages.settings.remoteCrewPanel.launch_done_unsigned')
            : i18nT('pages.settings.remoteCrewPanel.launch_done')
          : i18nT('pages.settings.remoteCrewPanel.runs_on_gateway')}
      </p>
    </Card>
  )
}

export function RemoteCrewPanel() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  // The panel's two tabs are the two THINGS a remote deployment can be, which is why
  // there are exactly two of them:
  //
  //   crews -- a gateway. A machine running `kirocrew gateway`: it serves a whole
  //            roster, it has a dashboard, and you switch to it from the top header.
  //            Reached over SSH, or over Session Manager to an EC2 instance.
  //   mates -- one agent. A Fargate task holding a single agent spec, serving a chat
  //            API, with no dashboard and no roster of its own.
  //
  // They shared one list before, and that is what made the list unreadable: a Fargate
  // row sat among machines wearing the same controls, while the one thing that told
  // them apart -- it has no dashboard, you can only chat to it -- was the third line of
  // a paragraph nobody finishes.
  const [tab, setTab] = useState<'crews' | 'mates'>('crews')
  // The EC2 launcher, which used to be this panel's whole second tab. It belongs to the
  // crews tab because what it creates is a crew, and it sits behind a button rather than
  // always open because it is a long form (an AWS probe, a size ladder, a subnet, an
  // identity) and the answer to "which crews do I have" must not be below all of it.
  const [showCrewLauncher, setShowCrewLauncher] = useState(false)
  // The mate picker, opened from the mates tab's footer.
  const [deployOpen, setDeployOpen] = useState(false)
  // The Members page's "Your crew in the cloud" dialog navigates here with `?deploy=1`.
  // It lands on CREWS and opens the crew launcher: that dialog is about the whole roster
  // (its own comment says "the deployment is the whole roster", and it draws every
  // member's face), so what it asks for is a gateway and not one agent.
  const [searchParams, setSearchParams] = useSearchParams()
  useEffect(() => {
    if (searchParams.get('deploy') !== '1') return
    setTab('crews')
    setShowCrewLauncher(true)
    // Consumed, not left in the URL: a reload, or a Back that returns here, must not
    // reopen a launch form the user already dismissed.
    const next = new URLSearchParams(searchParams)
    next.delete('deploy')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])
  // Default-on: when set, the web app auto-connects every crew on load and on
  // tab focus (see useAutoConnectInstances). Off lets a many-crew user stop the
  // per-load SSH + token-mint fan-out.
  const [autoConnect, setAutoConnect] = usePersistedBool(AUTO_CONNECT_KEY, true)

  // Setup-tab form + preflight state. `checkedProfile`/`checkedRegion` are the
  // committed values the preflight ran against, so typing a profile does not
  // hammer AWS on every keystroke — a check fires on first open, on blur, and on
  // the explicit Re-check.
  //
  // Both are persisted: this panel unmounts when you visit another Settings
  // section, and losing the profile meant more than retyping — `checkedProfile`
  // fell back to '', so the next probe silently tested the AWS CLI *default*
  // profile and reported someone else's expired credentials. The committed
  // mirrors seed from the same keys on this first render so the very first probe
  // uses the remembered account. Profile and region are names, not secrets —
  // the panel's own copy promises the profile NAME is all that is kept.
  const [profile, setProfile] = usePersistedString(CLOUD_PROFILE_KEY, '')
  const [region, setRegion] = usePersistedString(CLOUD_REGION_KEY, DEFAULT_REGION)
  const [checkedProfile, setCheckedProfile] = useState(() => readPersistedString(CLOUD_PROFILE_KEY, ''))
  const [checkedRegion, setCheckedRegion] = useState(() => readPersistedString(CLOUD_REGION_KEY, DEFAULT_REGION))
  // Opened when the REMEMBERED size lives inside it. Derived rather than persisted
  // on its own: the extra tiers are behind this disclosure, so a remembered x86
  // size with the section closed would drive the launch while its card was not on
  // screen — a worse failure than the reset, because nothing shows what is selected.
  const [showMoreSizes, setShowMoreSizes] = useState(
    () => X86_TIERS.some(t => t.key === readPersistedString(CLOUD_SIZE_KEY, 'balanced')),
  )
  const [persistedSize, setSizeKey] = usePersistedString(CLOUD_SIZE_KEY, 'balanced')
  // A value written by an older build — or naming a tier since removed — must not
  // select a tier that no longer exists: every card would render unselected while
  // the launch still carried the stale id.
  const sizeKey = (
    SIZE_TIERS.some(t => t.key === persistedSize) || X86_TIERS.some(t => t.key === persistedSize)
      ? persistedSize
      : 'balanced'
  ) as SizeTier['key']
  /**
   * The size a MATE launch sends: the tier's FAMILY, never an architecture variant.
   *
   * The mate picker asks for no size, so the value can only come from the crews tab's
   * remembered tier -- and that tier may be an x86 one (`balanced-x86`), which the
   * Fargate engine's `_tier_as_pair` does not know: it maps the three interactive tier
   * keys and nothing else. Sending the raw key would persist a job, answer 202, and then
   * raise inside `provision` on a size the mate dialog never offered. The family is what
   * the engine maps, and it is the same amount of machine either way -- the architecture
   * is the operator's own image, not a choice a Fargate launch makes.
   */
  const mateSizeKey =
    [...SIZE_TIERS, ...X86_TIERS].find(t => t.key === sizeKey)?.family ?? 'balanced'
  // The provisioner the setup tab draws. '' means "not chosen yet", which resolves
  // to the first renderable row below.
  const [persistedProvisioner, setProvisionerId] = usePersistedString(CLOUD_PROVISIONER_KEY, '')
  const [copied, setCopied] = useState<'command' | 'policy' | null>(null)
  // The Kiro identity the crew signs in as. Preselected from the launching
  // machine's own sign-in (an Identity Center user gets their organization's
  // portal, not the Builder ID one) and overridable; the server re-validates.
  // `identity_region` is the IAM Identity Center region — NOT the EC2 region.
  const [identityMode, setIdentityMode] = useState<'builder_id' | 'identity_center'>('builder_id')
  const [identityStartUrl, setIdentityStartUrl] = useState('')
  const [identityRegion, setIdentityRegion] = useState('')
  const [subnetId, setSubnetId] = useState('')
  const identityTouched = useRef(false)
  // Render-visible twin of the ref: an explicit choice must re-render the
  // Launch gate even when it re-selects the already-checked default.
  const [identityChosen, setIdentityChosen] = useState(false)
  const identityQuery = useQuery({
    queryKey: ['cloud-identity'],
    queryFn: api.cloudIdentity,
    staleTime: 60_000,
    retry: false,
  })
  useEffect(() => {
    // Seed ONCE from the inherited identity; never overwrite a user's edits.
    if (identityTouched.current) return
    const suggested = identityQuery.data?.suggested_target
    if (suggested?.start_url) {
      setIdentityMode('identity_center')
      setIdentityStartUrl(suggested.start_url)
    }
  }, [identityQuery.data])
  // Mirrors normalize_start_url on the backend, which is the authority: the
  // scheme may be omitted (https is assumed), the host is any valid DNS name
  // (Identity Center portals live in other partitions and on custom domains,
  // not only <org>.awsapps.com), and characters that would be unsafe on the
  // remote shell are refused. The form only decides whether Launch is enabled.
  const identityStartUrlOk = (() => {
    const v = identityStartUrl.trim()
    if (!v || /[\s"'`$\\;&|<>(){}[\]*?!~#]/.test(v)) return false
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(v) && !/^https:\/\//i.test(v)) return false // http:, ftp:, ...
    const bare = v.replace(/^https:\/\//i, '')
    return /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(\/[^?#]*)?$/i.test(bare)
  })()
  const identityRegionOk = /^[a-z]{2}(-[a-z]+)+-\d{1,2}$/.test(identityRegion.trim())
  // Until the launching computer's identity has been READ (or the user has
  // made a choice themselves), the Builder ID default is a placeholder, not a
  // decision: a returning operator with satisfied prerequisites could click
  // Launch inside that window and send no target, and an Identity Center
  // preselection that lands a moment later would have been ignored. A read
  // that could not answer -- the server reports `discovery: 'unknown'`, or the
  // request itself failed -- is the same placeholder: nothing is known about
  // this computer's sign-in, so the inline notice explains it and Launch waits
  // for the user to pick by hand. An Identity Center user whose whoami timed
  // out must not be launched as Builder ID by a preselection they never saw
  // was a guess.
  const identityUnknown = identityQuery.isError || identityQuery.data?.discovery === 'unknown'
  // The server reports the two unknown causes distinctly: `identity` is null
  // when whoami did not answer, and carries the Identity Center account type
  // when the sign-in was read but its portal address was not. The notice names
  // the one that happened rather than handing the user the disjunction.
  const identityUnknownCause: 'no_answer' | 'no_portal' | null =
    identityQuery.data?.discovery === 'unknown'
      ? identityQuery.data.identity?.account_type === 'IamIdentityCenter' ? 'no_portal' : 'no_answer'
      : null
  const identityResolving = (identityQuery.isPending || identityUnknown) && !identityChosen
  // While nothing is known and the user has not chosen, no radio renders
  // checked: a checked Builder ID beside a gate that says "choose" reads as a
  // choice already made. A read identity (or the user's own click) shows one.
  const identityRadioShown = identityChosen || !identityUnknown
  const identityOk =
    !identityResolving && (identityMode === 'builder_id' || (identityStartUrlOk && identityRegionOk))
  const loginTargetBody = identityMode === 'identity_center'
    ? { login_target: { license: 'pro', start_url: identityStartUrl.trim(), region: identityRegion.trim() } }
    : {}
  /** A failed copy, pinned to the checklist row whose button was pressed. */
  const [copyErr, setCopyErr] = useState<{ target: 'command' | 'policy'; message: string } | null>(null)
  const [activeLaunchId, setActiveLaunchId] = useState<string | null>(null)
  const [confirmDeleteTag, setConfirmDeleteTag] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  // Only one crew is editable at a time: two open forms on the same list would
  // let the user save conflicting ports without ever seeing the clash.
  const [editingId, setEditingId] = useState<string | null>(null)
  // Unsaved work in the open form. Swapping rows would unmount it and lose typed
  // host/port corrections silently, so the swap is refused instead.
  // The unsaved edit itself, keyed by crew — NOT a boolean. The form unmounts
  // whenever the crew list does (switching to the setup tab is enough) AND when
  // the error → agent hand-off navigates out of Settings entirely, and a guard can
  // only refuse the exits it knows about. Holding it in the store rather than in
  // this component means the work outlives every one of those exits without a
  // guard per exit, and without a serialised copy that would have to be
  // re-measured against a server record on the way back.
  // `seq` counts REBASES, and is used as the form's React key: adopting the current
  // record rewrites the draft's values, and a mounted form cannot re-seed itself.
  const editDraft = useAppSelector(s => s.instances.crewForms?.edit ?? null)
  // Which row's Edit was refused, not a bare flag: the refusal has to render at
  // the row the user actually clicked. Shown once at the bottom of the Card it
  // could sit off-screen in a long crew list, so the click looked like a no-op.
  const [editBlockedId, setEditBlockedId] = useState<string | null>(null)
  // Tags whose delete has been accepted by the gateway but not yet confirmed by AWS.
  // The DELETE endpoint returns `cleanup: "pending"` the moment the CloudFormation
  // delete is *requested* — the local registry row is only dropped minutes later, by
  // the gateway's background teardown watcher, once AWS reports DELETE_COMPLETE. Until
  // then the row is still returned by listInstances(), so without this the row simply
  // reappears unchanged after the click and looks like nothing happened. We remember
  // the tag to (a) show a "Deleting…" state on its row and (b) poll the list so the
  // row disappears on its own when the teardown finishes.
  const [deletingTags, setDeletingTags] = useState<Set<string>>(new Set())
  const [actionErr, setActionErr] = useState<string | null>(null)
  // A crew connected HERE that the gateway showing this page declined to adopt as
  // a tab of its own. Separate from `actionErr` because the connect succeeded:
  // folding it in would report a working crew as a failed connect.
  // Read from the module store, not from state set by a listener mounted here: the
  // host answers while the user is watching the crew connect, and this panel may
  // be closed by then. See `chainAnnounce.ts`.
  const chainRefusalNotice = useSyncExternalStore(
    subscribeChainRefusal,
    readChainRefusal,
    readChainRefusal,
  )
  const chainRefusal = chainRefusalNotice
    ? chainRefusalNotice.reason || i18nT('pages.settings.instancesPanel.unknown_error')
    : null
  // The last sign-in fetch/recheck outcome, for the job it belongs to. Rendered
  // inside that job's sign-in block, beside the button that produced it.
  const [signinNotice, setSigninNotice] = useState<({ jobId: string } & SigninNotice) | null>(null)
  // `kind` decides the surface: only `warn` (a negative ladder verdict, or the
  // tunnel's own `status.error`) is an error. `ok` / `info` describe a state that
  // has not gone wrong — healthy, or simply not connected yet — and render as a
  // status note, never as a red ErrorNotice with an agent hand-off. Mirrors
  // InstancesPanel's classification.
  const [diagNote, setDiagNote] = useState<{ kind: 'ok' | 'info' | 'warn'; text: string } | null>(null)
  // The diagnosis note's own report, so the hand-off carries the ladder's verdict
  // code and probe chain rather than the `id: reason` string on screen. Held as an
  // object because message text is not an identity: two crews unreachable the same
  // way produce byte-identical prose.
  const [diagReport, setDiagReport] = useState<ErrorReport | null>(null)
  const [restartPending, setRestartPending] = useState(false)

  const errMsg = useCallback(
    (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback),
    [],
  )

  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    // A delete only *requests* the teardown; the row is dropped later by the gateway's
    // background watcher once AWS confirms. Without polling the list would never
    // refetch again after the click's one invalidation, so the row would sit there
    // until an unrelated refetch. Poll while any delete is in flight, then stop.
    refetchInterval: () => (deletingTags.size > 0 ? 4000 : false),
  })
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
  /**
   * Which list a launch in flight belongs under, split the way the finished rows are.
   *
   * A job that named a mate is a mate being deployed; every other launch installs a
   * gateway. Unsplit, an EC2 crew launch drew a "Setting up" row under "Mates you can
   * chat to" and its presence also suppressed the "no mates yet" line, so the mates tab
   * claimed a mate was arriving that never would.
   */
  const pendingMates = useMemo(() => inProgress.filter(j => !!j.mate_name), [inProgress])
  const pendingCrews = useMemo(() => inProgress.filter(j => !j.mate_name), [inProgress])

  // An OLDER gateway POSIX-gates the read-only launch-history route too, so a
  // Windows host answers 400 posix_host_required for it. That is not a load
  // failure — the gateway is refusing a capability, not failing to read — and
  // letting it reach `loadError` below replaced the entire crew list, hand-added
  // SSH rows included, with a cloud-provisioning error and no way to connect,
  // edit or remove anything. Current gateways answer the route on every
  // platform; this keeps the panel usable against one that does not.
  //
  // Safe to proceed with no launch history — and it does NOT rest on the host
  // having no cloud crews, which a carried-over config dir would break. The row
  // itself does not assume: an SSM target with no matching job renders as
  // `unverifiedCloud`, keeping the confirm step and the copy about what Remove
  // does not do.
  const cloudUnsupported =
    launchesQuery.error instanceof ApiError &&
    launchesQuery.error.status === 400 &&
    parseErrorCode(launchesQuery.error.body) === 'posix_host_required'

  // Both queries gate the crew list: a row's cloud-vs-manual identity comes from the
  // launch history, and the two destructive actions are NOT interchangeable. Treating
  // absent launch data as [] makes a real cloud crew render as "added by you", whose
  // trash button is a single unconfirmed click that unregisters the instance and
  // leaves the EC2 stack running and billing, invisible to the dashboard. So the list
  // waits until both are known, and surfaces either failure instead of guessing.
  const loadError =
    !disabled && (instancesQuery.isError || (launchesQuery.isError && !cloudUnsupported))
  const authExpired =
    isAuthExpiredError(instancesQuery.error) || isAuthExpiredError(launchesQuery.error)
  const listLoading = !disabled && (instancesQuery.isLoading || launchesQuery.isLoading)

  // `activeLaunchId` is component state, so navigating away and back loses it while
  // the gateway keeps driving the job. Falling back to the persisted in-progress job
  // is what makes "Keeps running if you leave this page" true for the sign-in step:
  // without it the device code and verification link — the only way to finish setup —
  // are unreachable after a remount, and the user's only recovery is cancel-and-relaunch.
  //
  // A job that FINISHED without a confirmed sign-in needs the same treatment: the
  // gateway keeps its prompt alive on purpose (it clears job.signin only once sign-in
  // is confirmed), so restricting the fallback to in-progress jobs left the surviving
  // code unreachable — the crew is registered but never signed in, and the one screen
  // that could fix it renders nothing.
  //
  // Falling back to the newest persisted job (launches[0] — the API returns them
  // created-at descending) rather than only jobs that still carry a prompt: a launch
  // that FAILED or was reaped on restart has no signin, so gating on `!!j.signin`
  // hid its failure card after a reload — including the "check your crews, it may
  // still be running" warning for a stack that could still be billing.
  const effectiveLaunchId = activeLaunchId ?? inProgress[0]?.id ?? launches[0]?.id ?? null

  const launchStatusQuery = useQuery({
    queryKey: ['cloud', 'launch', effectiveLaunchId],
    queryFn: () => api.cloudLaunchStatus(effectiveLaunchId as string),
    enabled: !!effectiveLaunchId,
    refetchInterval: q => {
      const s = (q.state.data as LaunchJob | undefined)?.status
      return s && launchIsInFlight(s) ? 3000 : false
    },
  })

  // Which provisioners this gateway offers, and which of them this frontend can
  // draw. The stock build gets exactly one row (`aws_ec2`, drawn by the cards
  // below), so the selector never appears and this tab is unchanged. Fetched only
  // on the setup tab, because that is the only place it decides anything.
  const provisionersQuery = useQuery({
    queryKey: ['cloud', 'provisioners'],
    queryFn: () => api.cloudProvisioners(),
    // BOTH tabs need the lane list now: the crews tab's launcher resolves which
    // EC2-kind lane it is posting to, and the mates tab draws its Fargate card and its
    // picker chips from the same rows. Keyed on the feature being on, not on a tab.
    enabled: !disabled,
  })

  // Built-in first, then the edition's own in the order the server sent them: the
  // core-drawn launcher is the one every deployment has, so it is the default a
  // user who has never chosen lands on.
  const provisioners = useMemo(() => {
    const rows = provisionersQuery.data?.provisioners ?? []
    const drawable = rows.filter(p => canRenderRemoteProvisionerKind(p.kind))
    const builtin = (BUILTIN_REMOTE_PROVISIONER_KINDS as readonly string[])
    return [...drawable.filter(p => builtin.includes(p.kind)), ...drawable.filter(p => !builtin.includes(p.kind))]
  }, [provisionersQuery.data])

  // The lanes the CREW launcher may offer. A crew is a GATEWAY, so a lane that
  // deploys one agent is not one of the places a new crew can run: offering Fargate
  // under "where should the new crew run?" is the same category error as listing a
  // Fargate task among the crews. The mates tab resolves its own lane out of the
  // unfiltered list, which is why the filter lives here rather than in `provisioners`.
  const crewLanes = useMemo(
    () => provisioners.filter(p => p.kind !== FARGATE_PROVISIONER_ID),
    [provisioners],
  )

  // A remembered id the gateway no longer offers must not leave the tab drawing
  // nothing: fall back to the first renderable row, exactly as an unset choice
  // does. `null` while the list is unknown (loading or failed), which is what
  // keeps the built-in form the answer in those cases — the stock build never
  // depends on this query succeeding.
  const selectedProvisioner =
    crewLanes.find(p => p.id === persistedProvisioner) ?? crewLanes[0] ?? null
  // The user chose a lane the gateway has since stopped offering. Surfaced as a
  // line above the form rather than swallowed: the fallback puts them in front
  // of a different form, and a different bill, than the one they picked.
  // Measured against the FULL list: a remembered mate lane is not a lane that went
  // away, it is a lane that moved to the other tab, and saying "no longer offered"
  // about a row the mates tab is drawing at that moment would be false.
  const staleChoice =
    persistedProvisioner !== ''
    && provisioners.length > 0
    && !provisioners.some(p => p.id === persistedProvisioner)
  // The gateway answered and none of its lanes is one this frontend can draw as a
  // crew lane (an edition withdrew the built-in and this frontend predates its
  // renderer, or the only lane it offers deploys mates). Drawing the EC2 cards here
  // would offer a Launch that the server refuses with `unknown_provisioner`; a notice
  // says why there is nothing to launch.
  const noDrawableLane = provisionersQuery.isSuccess && crewLanes.length === 0
  // The fallback for an UNKNOWN list is the built-in form, so a failed or
  // still-loading query renders the EC2 cards rather than an empty tab.
  const builtinProvisioner =
    selectedProvisioner === null
    || (BUILTIN_REMOTE_PROVISIONER_KINDS as readonly string[]).includes(selectedProvisioner.kind)
  // The MATE lane, found in the unfiltered list rather than taken from
  // `selectedProvisioner`.
  //
  // That variable is the CREW launcher's own choice, and the launcher cannot pick this
  // lane at all now -- so reading it here reported "Fargate is not set up" directly
  // above a configured Fargate row. The two tabs ask different questions of the same
  // list, so each resolves its own answer from it.
  const mateLane = useMemo(
    () => provisioners.find(p => p.kind === FARGATE_PROVISIONER_ID) ?? null,
    [provisioners],
  )
  const registeredProvisioner = builtinProvisioner || selectedProvisioner === null
    ? undefined
    : getRemoteProvisionerRenderer(selectedProvisioner.kind)

  const preflightQuery = useQuery({
    queryKey: ['cloud', 'preflight', checkedProfile, checkedRegion],
    queryFn: () => api.cloudPreflight(checkedProfile || undefined, checkedRegion || undefined),
    // A user with no remembered lane is on the built-in form whatever the list
    // says, so the probe fires at once, as it always did. Only a remembered
    // choice waits for the list: it may resolve to a lane whose form must not
    // trigger an AWS probe at all.
    // Only while the EC2 launcher is actually open. It shells to `aws sts` and friends,
    // so merely opening the panel to read the crew list must not probe an AWS account.
    // The mate lane cannot be the launcher's choice, so there is no lane here whose
    // form must be kept away from the probe.
    enabled: showCrewLauncher && !disabled && !noDrawableLane
      && (persistedProvisioner === '' || (builtinProvisioner && !provisionersQuery.isLoading)),
  })

  const instances = useMemo(() => instancesQuery.data?.instances ?? [], [instancesQuery.data])
  const warmCap = instancesQuery.data?.warm_set_cap || WARM_SET_CAP_AUTO_CEILING
  // When the statuses on screen were READ, which is this query's own last success and
  // not a field on the record. A card that prints a time has to print a time it can
  // vouch for: a backend-stamped "checked at" would still be however old the poll that
  // delivered it is, and this is the number the reader is actually asking about when
  // they look at a green dot and wonder whether to trust it.
  const readAt = readClock(instancesQuery.dataUpdatedAt)
  // The two lists, split by WHAT each row is. One list mixed gateways and single agents
  // and gave them the same controls, which is the confusion this panel's two tabs exist
  // to end: a mate has no dashboard to connect to and no roster to switch between.
  const crewRows = useMemo(() => instances.filter(i => !isMate(i)), [instances])
  const mateRows = useMemo(() => instances.filter(i => isMate(i)), [instances])

  // A draft outlives its form ON PURPOSE, which means it can also outlive the CREW
  // it belongs to: Remove a crew mid-edit and the draft stays keyed by that id, so
  // adding a crew that lands on the same id (ids are derived from the name) would
  // remount the stale draft on a different machine and let Save overwrite settings
  // the user never typed. Anchored to the crew's EXISTENCE rather than to the
  // remove button, so a removal from the CLI, or a cloud Delete, clears it too.
  // Gated on a successful fetch: an errored poll must not be read as "all gone"
  // and throw away unsaved work.
  // Which of the draft's own fields no longer match the crew as it is PERSISTED.
  // The id staying alive is not proof the record did: a crew removed and recreated
  // under the same derived id between two polls never disappears from the list, and
  // a concurrent CLI edit moves the record without touching its id. Both make the
  // draft's baseline a description of something that no longer exists, so the form
  // is told and refuses to save until the user adopts the current record.
  const editExternallyChanged = useMemo(() => {
    if (editDraft === null) return []
    const live = instances.find(i => i.id === editDraft.id)
    if (live === undefined) return []
    const now = instanceFormFromView(live)
    const then = instanceFormFromView(editDraft.draft.baseline)
    // Only the fields that ADDRESS a machine. A label or lifetime someone changed
    // elsewhere cannot make this a different crew, and the baseline diff already
    // stops the save from reverting it — interrupting for that would spend the
    // user's attention on the case that was never dangerous.
    const identifying = ['method', 'sshHost', 'remotePort', 'ssmTarget', 'awsProfile', 'awsRegion'] as const
    return identifying.filter(k => now[k] !== then[k])
  }, [editDraft, instances])

  useEffect(() => {
    if (!instancesQuery.isSuccess) return
    const live = new Set(instances.map(i => i.id))
    if (editingId !== null && !live.has(editingId)) setEditingId(null)
    if (editDraft !== null && !live.has(editDraft.id)) dispatch(setCrewEditForm(null))
    setEditBlockedId(prev => (prev !== null && !live.has(prev) ? null : prev))
  }, [instances, instancesQuery.isSuccess, editingId, editDraft, dispatch])

  // Re-open the row whose edit is still held: a draft nobody re-mounts is the same
  // loss with an extra step. Runs whenever an unsaved edit exists with no form open
  // — arriving back from the hand-off, and equally after the setup tab unmounted the
  // list. No race with the user's own Cancel, and no guard for one: Cancel drops the
  // held values, so there is nothing left for this to re-open. That is the whole
  // benefit of one source of truth over a stored copy plus component state.
  useEffect(() => {
    if (!instancesQuery.isSuccess || editingId !== null || editDraft === null) return
    if (!instances.some(i => i.id === editDraft.id)) return
    setEditingId(editDraft.id)
  }, [instances, instancesQuery.isSuccess, editingId, editDraft])

  // instance_id → cloud tag, from every EC2 launch job that produced an instance.
  // An SSM instance whose target matches is a cloud crew, and this is its tag.
  // Built-in lane only: the Stop/Start/Delete this map unlocks call the EC2
  // routes, and a job from another provisioner names a resource those routes
  // cannot reach (a lane that needs lifecycle controls contributes its own).
  // A job with no `provider_id` at all can only come from a gateway older than
  // the field (a dev-server build against one); every such job WAS an EC2
  // launch, and dropping it here would let Remove unregister a crew whose stack
  // keeps billing.
  const cloudTagByInstanceId = useMemo(() => {
    const m = new Map<string, string>()
    for (const j of launches) {
      if (j.instance_id && (j.provider_id ?? BUILTIN_PROVISIONER_ID) === BUILTIN_PROVISIONER_ID) m.set(j.instance_id, j.tag)
    }
    return m
  }, [launches])
  /**
   * ECS target -> the mate a launch deployed there, which is the FACE that row wears.
   *
   * The registered NAME carries the launch tag, because two tasks of the same mate are
   * two rows and a reader has to tell them apart. The avatar must not follow that
   * suffix: one agent is one picture everywhere, and seeding from the row's name would
   * give the same agent a different face per launch. So the seed comes from the job's
   * own `mate_name`, and the name is free to disambiguate.
   */
  const mateNameByTarget = useMemo(() => {
    const m = new Map<string, string>()
    for (const j of launches) {
      if (j.instance_id && j.mate_name) m.set(j.instance_id, j.mate_name)
    }
    return m
  }, [launches])
  // instance_id → its launch job while the Kiro sign-in is missing or being
  // redone. Newest job per instance wins (the list is created-at descending), so
  // a crew re-signed by a later retry drops out once that retry confirms. The
  // polled copy of the active job outranks the list's snapshot of it: the list
  // is refetched on demand, the poll every few seconds, and the row must show the
  // code the moment the gateway publishes it.
  const signinJobByInstanceId = useMemo(() => {
    const m = new Map<string, LaunchJob>()
    for (const j of launches) {
      if (!j.instance_id || m.has(j.instance_id)) continue
      const live = launchStatusQuery.data?.id === j.id ? launchStatusQuery.data : j
      if (needsSignin(live) || (!isTerminal(live) && isRegistered(live))) m.set(j.instance_id, live)
    }
    return m
  }, [launches, launchStatusQuery.data])

  const reloadInstances = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['instances'] })
  }, [queryClient])
  const reloadLaunches = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'launches'] })
  }, [queryClient])

  // The other half of the announce. When the gateway showing this page refuses a
  // crew we announced, only IT holds the reason -- the depth cap and the cycle
  // guard are its decisions, taken against a registry this pane never sees -- so
  // without this the crew connects here and silently never appears up there. The
  // listener and the sender check live in `chainAnnounce.ts`, which catches the
  // answer whether or not this panel is open; `chainRefusal` above reads it.

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onMutate: () => { setActionErr(null); setDiagNote(null) },
    onSuccess: (st, id) => {
      if (st.state !== 'connected') { setActionErr(st.error || i18nT('pages.settings.instancesPanel.connection_did_not_complete_try_diagnose_for_det')); return }
      // Inside a pane, this gateway is itself a crew of the one showing the page,
      // so a crew connected here is reachable from up there only through our hop —
      // and the host never sees our registry. Tell it. A no-op at top level. The
      // notice carries no credential: the host mints its own over the credential
      // it already holds for us.
      const inst = instancesQuery.data?.instances?.find(i => i.id === id)
      // `hasDashboardPane` and not an inline transport test: it is the existing predicate
      // for this exact question and its own docstring gives the reason -- a fargate crew
      // "exposes a turn API on its forwarded port and nothing else: no dashboard, no
      // token", so it gets no switcher tab, no pane and no auto-connect. Every other
      // consumer already asks it; this announce was the one that did not, and a fargate
      // connect DOES supply `local_port`, so the port check alone let one through. The
      // host then persists the row before it ever tries to connect it, and its refusal
      // relay only fires when the gateway raises -- so the row survived as a crew
      // promising a tab that nothing can serve, removable only by hand.
      // `!inst.via_instance_id` for the depth cap, and it has to be asked HERE: the
      // host counts hops in its own registry, where our own hop to this crew is
      // invisible, so its `_chain_refusal` accepts the add and commits the row. Only
      // our gateway can see the extra hop, and it does -- `mint_embed_token` answers
      // `chain_too_deep` for exactly this row -- but by then the host has persisted
      // it, and the refusal relay leaves the row behind. The same field the backend
      // refuses on, so the two ends agree on what "one hop too far" means.
      if (inst && st.local_port && hasDashboardPane(inst) && !inst.via_instance_id) {
        announceChainedCrew({
          id,
          name: inst.name,
          sshHost: inst.connection_method === 'ssh' ? inst.ssh_host : inst.ssm_target,
          remotePort: inst.remote_port,
          port: st.local_port,
        })
      }
    },
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
  /**
   * "Check again" on ONE row: re-read that row's tunnel status and nothing else.
   *
   * It called `reloadInstances`, which is exactly what the card header's Refresh does --
   * two controls for one action, and the wrong one for the question being asked. The
   * button sits beside a single row's stale reading, so it re-reads that row through
   * `GET /api/instances/{id}/status` and writes the answer into the cached list. No
   * `diagnose=1`: that runs the probe ladder, which is what the kebab's Diagnose is for.
   */
  const recheckMutation = useMutation({
    mutationFn: (id: string) => api.instanceStatus(id),
    onSuccess: (status, id) => {
      queryClient.setQueryData(
        ['instances'],
        (prev: { instances: InstanceView[] } | undefined) =>
          prev
            ? { ...prev, instances: prev.instances.map(i => (i.id === id ? { ...i, status } : i)) }
            : prev,
      )
    },
    // A refused read leaves the old reading and its age standing, which is the honest
    // state: nothing newer was learned. The row's own error surface reports it.
    onError: (e, id) => setActionErr(i18nT('pages.settings.remoteCrewPanel.row_recheck_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
  })
  const diagnoseMutation = useMutation({
    mutationFn: (id: string) => api.instanceStatus(id, true),
    onMutate: () => { setActionErr(null); setDiagNote(null); setDiagReport(null) },
    onSuccess: (st, id) => {
      const code = st.diagnosis?.code
      // Two verdicts are BENIGN: `ok`, and `not_connected` — which is `ok: false`
      // on the wire but whose reason is guidance ("click Connect"), not a failure.
      // Neither may label an error surface or reach the failure report.
      const benign = code === 'ok' || code === 'not_connected'
      const failing = st.diagnosis && !benign ? st.diagnosis : undefined
      // Displayed text, most specific first: a FAILING ladder verdict names the
      // broken link, so it wins; otherwise the tunnel's live `status.error`; and
      // only then a benign verdict's own reason. The ladder result is the last
      // RUN, so a stale "All checks passed" / "click Connect" must never label a
      // red notice whose real cause is the live error.
      const reason = failing?.reason || st.error || st.diagnosis?.reason
      // A benign verdict is only benign while the tunnel has no error of its own.
      const kind: 'ok' | 'info' | 'warn' =
        st.error || failing ? 'warn' : code === 'ok' ? 'ok' : code === 'not_connected' ? 'info' : 'warn'
      if (reason) setDiagNote({ kind, text: `${id}: ${reason}` })
      // Journal unconditionally, healthy verdict included: the recorder's
      // no-failure path is what clears its de-dup signature, so skipping the call
      // on a healthy diagnose would leave the signature standing and suppress the
      // next identical failure. It returns null when there is nothing to describe.
      // A benign verdict is stripped from the status handed over: the recorder
      // treats any not-ok verdict as a failure, so `not_connected` would otherwise
      // be journaled as a system error (the #11110 defect by another path), and a
      // stale benign verdict beside a live error would decorate that error's
      // report with a probe chain that says nothing is wrong.
      const inst = instances.find(i => i.id === id)
      const { diagnosis: _omitted, ...withoutDiagnosis } = st
      setDiagReport(reportInstanceFailure({
        id,
        name: inst?.name || id,
        transport: inst && usesSsmTransport(inst) ? 'ssm' : 'ssh',
        status: benign ? withoutDiagnosis : st,
        stage: 'connect',
        fallbackMessage: kind === 'warn' ? reason || '' : '',
      }))
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
  const startMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudStart(v.tag, v.coords),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  const deleteMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudDestroy(v.tag, v.coords),
    onMutate: () => { setActionErr(null); setConfirmDeleteTag(null) },
    // The request only *starts* the teardown (the gateway returns cleanup: "pending");
    // remember the tag so its row shows "Deleting…" and the list polls until the
    // background watcher drops the row once AWS confirms.
    onSuccess: (_r, v) => setDeletingTags(prev => new Set(prev).add(v.tag)),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  // Fetches the pending device-code prompt for a job that is already awaiting
  // sign-in. Distinct from the restart below: this asks for the code that exists,
  // that one asks for a new one. Keyed by the job the click named, not by
  // `activeLaunchId` — the crew row offers this too, and there the job being
  // fetched may not be the active launch at all.
  // The job id a row's sign-in buttons act on, or null when it has none. Used to
  // scope the shared mutations' pending state to the row that was clicked.
  const signinJobIdFor = (inst: InstanceView): string | null =>
    inst.connection_method === 'ssm' && inst.ssm_target
      ? signinJobByInstanceId.get(inst.ssm_target)?.id ?? null
      : null
  const signinMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchSignin(id),
    onMutate: () => { setActionErr(null); setSigninNotice(null) },
    onError: (e, id) => {
      // "I approved it -- check now" SUCCEEDS as HTTP 409 `signin_already_complete`:
      // the gateway re-probed the box, found the sign-in, and there is no prompt
      // left to return. That is the outcome the user clicked for, not an error.
      // Painting it red told a signed-in user their sign-in had failed.
      const code = e instanceof ApiError && e.status === 409 ? parseErrorCode(e.body) : null
      if (code === 'signin_already_complete') {
        reloadInstances()
        reloadLaunches()
        return
      }
      // The other 409: the box was re-probed and the approval has NOT landed
      // (clicked early, or not yet propagated). The ordinary recheck outcome,
      // not a failure -- and a re-render of the same screen is not an answer, so
      // the block says "not signed in yet" beside the button.
      if (code === 'no_signin_pending') {
        setSigninNotice({ jobId: id, kind: 'not_signed_in_yet' })
        reloadLaunches()
        return
      }
      // Anything else: say so next to the button that was clicked, not only in
      // the panel banner above the size cards.
      setSigninNotice({ jobId: id, kind: 'request_failed', detail: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })
    },
    onSettled: (_r, _e, id) => { void queryClient.invalidateQueries({ queryKey: ['cloud', 'launch', id] }) },
  })
  // Starts the Kiro sign-in again on a crew that ended up without one. The job
  // becomes the active launch so its card and its row poll the new code live.
  const signinRestartMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchSigninRestart(id),
    onMutate: () => { setActionErr(null); setSigninNotice(null) },
    onSuccess: job => { setActiveLaunchId(job.id); reloadLaunches() },
    onError: e => {
      // The route refuses a restart on a crew that is ALREADY signed in with 409
      // `signin_already_complete`. That is the good outcome -- another tab, or the
      // re-probe, confirmed the sign-in -- and painting it red told the reader
      // their crew had failed to sign in when it had just succeeded. Reload so the
      // badge and Connect catch up, and say nothing.
      if (e instanceof ApiError && e.status === 409 && parseErrorCode(e.body) === 'signin_already_complete') {
        reloadInstances()
        reloadLaunches()
        return
      }
      setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')))
    },
    onSettled: (_r, _e, id) => { void queryClient.invalidateQueries({ queryKey: ['cloud', 'launch', id] }) },
  })
  // Takes its body as VARIABLES rather than closing over the form state: a
  // registered provisioner's form owns its own inputs, and the core cannot read
  // them. The built-in call site passes the panel's own profile/region/size.
  const launchMutation = useMutation({
    mutationFn: (body: {
      provider_id?: string
      profile: string
      region: string
      size_key: string
      subnet_id?: string
      // The credential recipient the operator confirmed, sent VERBATIM from the lane's
      // own `confirm_before_launch`. The client never composes it: the server compares
      // it to the value that descriptor published and the engine compares it again to
      // what it is about to launch, so a value this dashboard invented would be refused
      // rather than honoured.
      confirm_recipient?: string
      // Which crew this launch is for. Optional, so the EC2 form's body is unchanged.
      mate_name?: string
    }) =>
      api.cloudLaunch(body),
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
        // These two take {tag, coords}, so interpolating `variables` directly yielded
        // "stop:[object Object]" — a key no row could ever match, leaving the button
        // label stuck on "Stop" for the whole request. (The row still disabled, since
        // that only tests `!!busy`, which is why this stayed invisible.)
        ? `stop:${stopMutation.variables?.tag}`
        : startMutation.isPending
          ? `start:${startMutation.variables?.tag}`
          : disconnectMutation.isPending || removeMutation.isPending || deleteMutation.isPending
            ? 'busy'
            : ''

  const runCheck = useCallback(() => {
    setCheckedProfile(profile)
    setCheckedRegion(region)
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'preflight'] })
  }, [profile, region, queryClient])

  // Both copies branch on the boolean `copyToClipboard` returns: a denied
  // clipboard write used to paint "Copied" regardless, which is a false
  // confirmation on exactly the command the user is about to need. Each failure
  // is keyed to its own button so it renders beside it (the checklist sits well
  // below the page-level notices), and the two messages differ because the
  // command IS on screen to select by hand while the policy JSON never is.
  const copyCommand = useCallback(async (command: string) => {
    setCopyErr(null)
    // try/catch as well as the boolean: the helper resolves `false` when the
    // `execCommand` fallback reports failure, but REJECTS when that fallback
    // throws, and this callback is fire-and-forget at its call site — an
    // unhandled rejection would be a copy that failed with no notice.
    let ok = false
    try {
      ok = await copyToClipboard(command)
    } catch {
      ok = false
    }
    if (!ok) {
      setCopyErr({ target: 'command', message: i18nT('pages.settings.remoteCrewPanel.copy_failed') })
      return
    }
    setCopied('command')
    setTimeout(() => setCopied(null), 1500)
  }, [])
  const copyPolicy = useCallback(async () => {
    setCopyErr(null)
    try {
      const { policy } = await api.cloudIamPolicy()
      if (!(await copyToClipboard(policy))) {
        setCopyErr({ target: 'policy', message: i18nT('pages.settings.remoteCrewPanel.copy_policy_failed') })
        return
      }
      setCopied('policy')
      setTimeout(() => setCopied(null), 1500)
    } catch (e) {
      setCopyErr({ target: 'policy', message: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })
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

  // Once a teardown finishes the gateway drops the instance, so its row (and its
  // "Deleting…" state) vanishes on the next poll. Prune the tag then, which also
  // stops the poll once nothing is deleting. Keyed off the instance list so it fires
  // exactly when a row disappears rather than on a timer.
  useEffect(() => {
    if (deletingTags.size === 0) return
    const liveTags = new Set(
      instances
        .map(i => (i.ssm_target ? cloudTagByInstanceId.get(i.ssm_target) : undefined))
        .filter((t): t is string => !!t),
    )
    const next = new Set([...deletingTags].filter(t => liveTags.has(t)))
    if (next.size !== deletingTags.size) setDeletingTags(next)
  }, [instances, cloudTagByInstanceId, deletingTags])

  // ── Initial load: don't render the full UI until we know whether the
  //    feature is enabled. Without this the panel flashes the tabbed form
  //    and then jitters to the "off" card once the 403 arrives. ──
  if (instancesQuery.isLoading) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-muted text-sm py-2">
          <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.instancesPanel.loading')}
        </div>
      </Card>
    )
  }

  // ── Disabled feature gate (mirrors InstancesPanel) ──
  if (disabled) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-text font-medium mb-1" data-setting-label={i18nT('pages.settings.instancesPanel.enable_remote_crew_management')}>
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
        <ErrorNotice message={actionErr} askAgent className="mt-2" />
      </Card>
    )
  }

  // ONE row renderer, called by BOTH lists. The crews tab and the mates tab draw the
  // same card -- a row is a row whichever of the two it is, and only the avatar differs
  // (see `mate` in CrewRow) -- so a second copy of these nineteen props would be two
  // places for a prop to be forgotten rather than one.
  const renderRow = (inst: InstanceView) => (
              <CrewRow
                key={inst.id}
                inst={inst}
                cloudTag={inst.connection_method === 'ssm' && inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) ?? null : null}
                avatarSeed={inst.ssm_target ? mateNameByTarget.get(inst.ssm_target) ?? '' : ''}
                signinJob={inst.connection_method === 'ssm' && inst.ssm_target ? signinJobByInstanceId.get(inst.ssm_target) ?? null : null}
                onRestartSignin={id => signinRestartMutation.mutate(id)}
                // Keyed to THIS row's job: the mutation is one object shared by
                // every row, so its bare `isPending` would disable the sign-in
                // buttons on every other unsigned instance the moment one is
                // clicked -- reading as if the whole panel were busy.
                restartingSignin={signinRestartMutation.isPending && signinRestartMutation.variables === signinJobIdFor(inst)}
                onFetchSignin={id => signinMutation.mutate(id)}
                fetchingSignin={signinMutation.isPending && signinMutation.variables === signinJobIdFor(inst)}
                signinNotice={signinNotice && signinNotice.jobId === signinJobIdFor(inst) ? signinNotice : null}
                readAt={readAt}
                onCheckAgain={() => recheckMutation.mutate(inst.id)}
                busy={busy}
                deleting={inst.ssm_target ? deletingTags.has(cloudTagByInstanceId.get(inst.ssm_target) ?? '') : false}
                confirmDelete={confirmDeleteTag !== null && confirmDeleteTag === (inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) : null)}
                confirmRemove={confirmRemoveId === inst.id}
                onConnect={id => connectMutation.mutate(id)}
                onDisconnect={id => disconnectMutation.mutate(id)}
                onDiagnose={id => diagnoseMutation.mutate(id)}
                onRemove={id => removeMutation.mutate(id)}
                onStop={(tag, coords) => stopMutation.mutate({ tag, coords })}
                onStart={(tag, coords) => startMutation.mutate({ tag, coords })}
                onDelete={(tag, coords) => deleteMutation.mutate({ tag, coords })}
                onRequestDelete={tag => setConfirmDeleteTag(tag)}
                onRequestRemove={id => setConfirmRemoveId(id)}
                editing={editingId === inst.id}
                blocked={editBlockedId === inst.id}
                onEdit={id => {
                  // Switching rows would unmount another crew's draft.
                  if (
                    id !== null
                    && editDraft !== null
                    && id !== editDraft.id
                  ) {
                    setEditBlockedId(id)
                    return
                  }
                  setEditBlockedId(null)
                  // Cancel (id === null) is the user CHOOSING to discard; the draft
                  // goes with it. Every other way the form disappears keeps it.
                  if (id === null) dispatch(setCrewEditForm(null))
                  setEditingId(id)
                }}
                editDraft={editDraft?.id === inst.id ? editDraft.draft : null}
                editExternallyChanged={editDraft?.id === inst.id ? editExternallyChanged : []}
                // A three-way merge, with the old baseline as the merge base: the
                // user's TYPED fields are kept, and every field they did not touch
                // is taken from the record that actually exists. Keeping all the old
                // values instead would turn untouched-but-stale fields into
                // deliberate writes — the exact clobber the baseline exists to stop.
                editDraftSeq={editDraft?.id === inst.id ? editDraft.seq : 0}
                onEditRebase={() => {
                  if (editDraft === null || editDraft.id !== inst.id) return
                  const base = instanceFormFromView(editDraft.draft.baseline)
                  const live = instanceFormFromView(inst)
                  const merged = { ...live }
                  for (const k of Object.keys(base) as (keyof typeof base)[]) {
                    if (editDraft.draft.values[k] === base[k]) continue
                    // Field-wise assign: the value's type is the field's own, and
                    // a generic index write cannot see that.
                    Object.assign(merged, { [k]: editDraft.draft.values[k] })
                  }
                  dispatch(setCrewEditForm({
                    id: inst.id,
                    draft: { values: merged, baseline: inst },
                    seq: editDraft.seq + 1,
                  }))
                }}
                onEditDraftChange={draft => {
                  const next =
                    draft === null
                      ? null
                      : {
                          id: inst.id, draft,
                          seq: editDraft?.id === inst.id ? editDraft.seq : 0,
                        }
                  // Same values, same action: the report fires on every keystroke,
                  // and dispatching an equal-but-new object re-renders for nothing.
                  if (JSON.stringify(editDraft) === JSON.stringify(next)) return
                  if (next === null) setEditBlockedId(null)
                  dispatch(setCrewEditForm(next))
                }}
                // Clearing editingId without clearing the refusal left the UI
                // instructing the user about a form that no longer exists.
                onEditSaved={updated => {
                  setEditingId(null)
                  dispatch(setCrewEditForm(null))
                  setEditBlockedId(null)
                  // A warm pane is an iframe pointed at the OLD local port with the
                  // OLD token. If the save tore the tunnel down (any transport
                  // field changed), that pane cannot be revived by reconnecting —
                  // it would reuse a credential the new tunnel never issued and sit
                  // on 403. Drop it so the next Connect builds a fresh one. A
                  // name-or-ttl-only edit leaves the tunnel up, and its pane keeps
                  // working, so it is deliberately NOT dropped.
                  if (updated.status?.state !== 'connected') dispatch(removeWarm(inst.id))
                  reloadInstances()
                }}
              />
  )
  // Auto-connect is a CREW setting: it opens tunnels to gateways on page load. A mate is
  // reached per chat and has no tunnel to pre-open, so this stays on the crews tab.
  const autoConnectCard = (
    <Card>
      <SettingsToggle
        label={i18nT('pages.settings.remoteCrewPanel.auto_connect')}
        description={i18nT('pages.settings.remoteCrewPanel.auto_connect_desc')}
        checked={autoConnect}
        onChange={setAutoConnect}
      />
    </Card>
  )
  // The list card, drawn by BOTH tabs. `rows` is the split list; the three things that
  // differ between a gateway and an agent -- the heading, the empty line and the footer
  // action -- are passed in rather than branched on inside, so neither tab can quietly
  // grow a control the other does not have.
  const rowsCard = ({ rows, pending, heading, what, empty, action, warmLine = false }: {
    rows: InstanceView[]
    /** The launches in flight that belong to THIS list, not the panel's whole set. */
    pending: LaunchJob[]
    heading: string
    /** One line under the heading saying what KIND of thing this list holds. The same
     *  sentence the footer button carries, said where a reader lands rather than only at
     *  the foot of the list: the two tabs differ in what their rows ARE, and a heading
     *  naming crews or mates assumes the distinction the reader is here to learn. */
    what: string
    empty: string
    action: ReactNode
    /** Whether to print the warm-set cap. The cap governs CREW tunnels, so the mates
     *  list leaves it off rather than naming a concept it does not answer to. */
    warmLine?: boolean
  }) => (
    <Card>
      <div className="flex items-center justify-between mb-1">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-text font-medium">
            <Server className="lucide-inline" /> {heading}
          </div>
          <div className="text-[11.5px] text-muted-strong mt-0.5">{what}</div>
        </div>
        <div className="flex items-center gap-2">
          {warmLine && (
            <span className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.up_to_warm', { n: warmCap })}</span>
          )}
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
          {/* No hand-off: the add-crew form shares this tab — a failed
              list refresh must not offer a navigation that discards it. */}
          <ErrorNotice message={errMsg(instancesQuery.error ?? launchesQuery.error, i18nT('pages.settings.instancesPanel.unknown_error'))} />
          {/* Refresh replays the same rejected credential, so it can only
              reproduce the error until the user re-authenticates through
              the banner the notice points at. */}
          {!authExpired && (
            <Btn className="mt-2" onClick={() => { reloadInstances(); reloadLaunches() }}>
              <RefreshCw className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.refresh')}
            </Btn>
          )}
        </div>
      ) : (
        <div>
          {/* A sign-in retry is also in progress, but its crew is already a
              row below (the connect step ran); a second "Setting up" row
              would read as a second instance being created — and billed. */}
          {pending.filter(job => !isRegistered(job)).map(job => (
            <SettingUpRow key={job.id} job={job} />
          ))}
          {rows.map(renderRow)}
          {pending.length === 0 && rows.length === 0 && (
            <div className="text-[13px] text-muted py-1">{empty}</div>
          )}
        </div>
      )}
      {confirmDeleteTag !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.delete_warning')}</p>}
      {confirmRemoveId !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.remove_warning')}</p>}
      {/* The way IN, at the foot of the list the thing lives in. It used to be on the
          other tab, behind an AWS prerequisite checklist and a size ladder -- which is a
          setup form, not an answer to "put this in the cloud" -- and it never asked WHICH
          crew or mate at all. Each tab passes its own, because the two create different
          things: a gateway, or one agent. */}
      <div className="mt-3.5 pt-3 border-t border-border flex items-center gap-2.5 flex-wrap">
        {action}
      </div>
    </Card>
  )
  // The EC2 launcher: the whole of what used to be this panel's second tab. It creates a
  // CREW -- a gateway on a fresh machine -- so it lives on the crews tab, revealed by
  // that tab's footer button rather than always open.
  const crewLauncher = (
  <div
    className="space-y-4"
    id="crew-launcher-panel"
    role="region"
    aria-label={i18nT('pages.settings.remoteCrewPanel.deploy_crew_title')}
  >
    {/* The lane list could not be read. The built-in form below still
        renders (the list is presentation, not permission), but the
        failure is said rather than swallowed: a newer dashboard on an
        older gateway is exactly the version skew the agent can explain.
        askAgent ON: the launch form persists its account and size, so
        the navigation destroys nothing. */}
    {provisionersQuery.isError && (
      <ErrorNotice
        message={errMsg(provisionersQuery.error, i18nT('pages.settings.remoteCrewPanel.provisioners_unavailable'))}
        askAgent
      />
    )}

    {/* Which provisioner, out of the CREW lanes only. Rendered when more than one of
        them is drawable, so the stock build shows nothing here and goes straight to
        the AWS cards below -- and so does a build whose only other lane deploys
        mates, because a mate lane is not an answer to this question. */}
    {crewLanes.length > 1 && (
      <Card>
        <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.provisioner_choose')}</div>
        <div className="space-y-2.5">
          {crewLanes.map(p => (
            <ProvisionerCard
              key={p.id}
              provisioner={p}
              on={selectedProvisioner?.id === p.id}
              onPick={setProvisionerId}
            />
          ))}
        </div>
      </Card>
    )}

    {/* The remembered lane is gone; say so before showing a different form. */}
    {staleChoice && selectedProvisioner && (
      <p role="status" className="text-[12px] text-muted flex items-start gap-1.5">
        <AlertTriangle size={13} className="mt-0.5 shrink-0 text-warn" />
        {i18nT('pages.settings.remoteCrewPanel.provisioner_stale_choice', { label: selectedProvisioner.label })}
      </p>
    )}

    {noDrawableLane ? (
      <Card>
        <div className="text-[13px] text-muted">{i18nT('pages.settings.remoteCrewPanel.provisioner_none_drawable')}</div>
      </Card>
    ) : builtinProvisioner ? (
    <>
    {/* AWS prerequisites — the account inputs live HERE, above the rows they
        produce. The check runs against this profile/region, so showing the
        verdict first and the inputs in a later card inverted cause and effect:
        a red "credentials expired" row gave no hint that it had probed a
        different profile than the one the reader had in mind. These same two
        values are also what the launch below uses. */}
    <Card>
      <div className="flex items-center gap-2 mb-3 text-text font-medium">
        <CheckCircle className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.before_you_start')}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
        <label htmlFor="cloud-profile" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.aws_profile')}
          <input
            id="cloud-profile"
            aria-label={i18nT('pages.settings.instancesPanel.aws_profile')}
            className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring"
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
            className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring"
            value={region}
            onChange={e => setRegion(e.target.value)}
            onBlur={runCheck}
            placeholder="us-east-1"
          />
        </label>
      </div>
      <p className="mb-3 text-[12px] text-muted">
        {i18nT('pages.settings.remoteCrewPanel.checking_against', {
          profile: checkedProfile || i18nT('pages.settings.remoteCrewPanel.no_profile_set'),
          region: checkedRegion,
        })}
      </p>
      {/* Rendered independently of `preflight`: a failed RE-check keeps the
          previous result in cache, and the rows below would otherwise paint
          that stale verdict as if the check had just passed. askAgent ON:
          the launch form on this tab keeps its size and account in
          localStorage (see the diagnosis notice above), so the navigation
          destroys nothing, and a preflight that cannot even run is exactly
          the credential/CLI problem the agent can look into. */}
      {preflightQuery.isError && (
        <ErrorNotice
          className="mb-2"
          message={errMsg(preflightQuery.error, i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
          askAgent
        />
      )}
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
              ? i18nT('pages.settings.remoteCrewPanel.credentials_ok', { profile: checkedProfile || i18nT('pages.settings.remoteCrewPanel.no_profile_set'), account: preflight.account })
              : (preflight.detail || i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
            onRecheck={runCheck}
            rechecking={preflightQuery.isFetching}
          />
          <PrereqRow ok={preflight.ec2_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ec2')} detail={preflight.ec2_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
          <PrereqRow ok={preflight.cloudformation_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_cloudformation')} detail={preflight.cloudformation_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} extraAction={preflight.cloudformation_reachable ? undefined : <Btn onClick={copyPolicy}>{copied === 'policy' ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied === 'policy' ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_policy_json')}</Btn>} error={copyErr?.target === 'policy' ? copyErr.message : undefined} />
          <PrereqRow ok={preflight.ssm_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ssm')} detail={preflight.ssm_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
          <PrereqRow
            ok={preflight.session_manager_plugin}
            title={i18nT('pages.settings.remoteCrewPanel.plugin')}
            detail={preflight.session_manager_plugin ? i18nT('pages.settings.remoteCrewPanel.plugin_ok') : i18nT('pages.settings.remoteCrewPanel.plugin_missing')}
            command={preflight.session_manager_plugin ? undefined : (preflight.session_manager_plugin_command || undefined)}
            onCopyCommand={
              preflight.session_manager_plugin || !preflight.session_manager_plugin_command
                ? undefined
                : () => { void copyCommand(preflight.session_manager_plugin_command as string) }
            }
            copied={copied === 'command'}
            error={copyErr?.target === 'command' ? copyErr.message : undefined}
            onRecheck={preflight.session_manager_plugin ? undefined : runCheck}
            rechecking={preflightQuery.isFetching}
          />
        </ul>
      ) : (
        <div className="text-[13px] text-muted py-1">
          {/* The failure itself renders above, independent of this branch;
              this is the no-result state with its Re-check. */}
          {i18nT('pages.settings.remoteCrewPanel.credentials_bad')}
          <div className="mt-2"><Btn onClick={runCheck} disabled={preflightQuery.isFetching}><RefreshCw className={`lucide-inline${preflightQuery.isFetching ? ' animate-spin' : ''}`} /> {preflightQuery.isFetching ? i18nT('pages.settings.remoteCrewPanel.checking') : i18nT('pages.settings.remoteCrewPanel.re_check')}</Btn></div>
        </div>
      )}
      <p className="mt-3 text-[12px] text-muted flex items-start gap-1.5">
        <CheckCircle size={13} className="mt-0.5 shrink-0 text-ok" /> {i18nT('pages.settings.remoteCrewPanel.profile_name_only')}
      </p>
    </Card>

    {/* Launch form — profile/region are set in the prerequisites card above,
        which is the same account this launches into. */}
    <Card>
      <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.new_cloud_crew')}</div>

      <div>
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

      <label className="mt-4 block text-[13px] text-muted">
        {i18nT('pages.settings.remoteCrewPanel.subnet')}
        <input
          type="text"
          value={subnetId}
          aria-label={i18nT('pages.settings.remoteCrewPanel.subnet')}
          onChange={e => setSubnetId(e.target.value)}
          placeholder="subnet-0123456789abcdef0"
          spellCheck={false}
          className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
        />
        <span className="block mt-1 text-[12px]">{i18nT('pages.settings.remoteCrewPanel.subnet_hint')}</span>
      </label>

      <div className="mt-4">
        <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.identity')}</div>
        {identityQuery.isError ? (
          <>
            {/* No hand-off: this notice sits beside the unsaved Identity Center
                start-URL and region draft, and the hand-off navigates to chat,
                which would unmount the form and discard that draft. The failure is
                non-blocking — the only consequence is that the launching computer's
                identity could not be read to preselect the radios; the user can
                still pick and type the target below. */}
            <ErrorNotice
              variant="inline"
              className="mb-2"
              message={i18nT('pages.settings.remoteCrewPanel.identity_lookup_failed')}
            />
          </>
        ) : identityUnknownCause !== null ? (
          <>
            {/* No hand-off: like the notice above, this sits beside the unsaved
                Identity Center start-URL and region draft, and a hand-off navigates
                to chat, unmounting the form and discarding that draft. The server
                suggests nothing, so no radio is checked until the user picks, and
                Launch waits for that pick. The notice names the cause the server
                reported: whoami did not answer, or an Identity Center sign-in was
                read without its portal address. */}
            <ErrorNotice
              variant="inline"
              className="mb-2"
              message={i18nT(
                identityUnknownCause === 'no_portal'
                  ? 'pages.settings.remoteCrewPanel.identity_unknown_no_portal'
                  : 'pages.settings.remoteCrewPanel.identity_unknown_no_answer',
              )}
            />
          </>
        ) : null}
        <div className="space-y-2">
          <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
            <input
              type="radio"
              name="kiro-identity"
              className="mt-0.5"
              checked={identityRadioShown && identityMode === 'builder_id'}
              aria-label={i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
              onChange={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('builder_id') }}
              // Re-selecting the already-checked default fires no change
              // event, but it IS the user's explicit choice, and that
              // choice ends the wait for the inherited identity.
              onClick={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('builder_id') }}
            />
            <span>
              {i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
              <span className="block text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.identity_builder_id_hint')}</span>
            </span>
          </label>
          <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
            <input
              type="radio"
              name="kiro-identity"
              className="mt-0.5"
              checked={identityRadioShown && identityMode === 'identity_center'}
              aria-label={i18nT('pages.settings.remoteCrewPanel.identity_center')}
              onChange={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('identity_center') }}
            />
            <span>
              {i18nT('pages.settings.remoteCrewPanel.identity_center')}
              <span className="block text-[12px] text-muted">
                {identityQuery.data?.discovery !== 'unknown' && identityQuery.data?.identity?.account_type === 'IamIdentityCenter'
                  ? i18nT('pages.settings.remoteCrewPanel.identity_center_inherited')
                  : i18nT('pages.settings.remoteCrewPanel.identity_center_hint')}
              </span>
            </span>
          </label>
          {identityMode === 'identity_center' && (
            <div className="ml-6 space-y-2">
              <label className="block text-[12px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                <input
                  type="url"
                  value={identityStartUrl}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                  onChange={e => { identityTouched.current = true; setIdentityChosen(true); setIdentityStartUrl(e.target.value) }}
                  placeholder="https://example.awsapps.com/start"
                  spellCheck={false}
                  aria-invalid={identityStartUrl !== '' && !identityStartUrlOk}
                  className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
                />
              </label>
              <label className="block text-[12px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.identity_region')}
                <input
                  type="text"
                  value={identityRegion}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.identity_region')}
                  onChange={e => { identityTouched.current = true; setIdentityChosen(true); setIdentityRegion(e.target.value) }}
                  placeholder="us-east-1"
                  spellCheck={false}
                  aria-invalid={identityRegion !== '' && !identityRegionOk}
                  className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
                />
                <span className="block mt-1">{i18nT('pages.settings.remoteCrewPanel.identity_region_hint')}</span>
              </label>
            </div>
          )}
        </div>
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
        {/* Name the lane that is on screen. The built-in form draws EVERY
            `aws_ec2`-kind row, and an edition may register a second one
            behind a different engine; omitting the id would let the server
            default to the built-in and provision on the wrong lane. Only
            an UNKNOWN list (loading or failed) sends the pre-seam body. */}
        <Btn primary onClick={() => launchMutation.mutate({ ...(selectedProvisioner ? { provider_id: selectedProvisioner.id } : {}), profile, region, size_key: sizeKey, ...(subnetId.trim() ? { subnet_id: subnetId.trim() } : {}), ...loginTargetBody })} disabled={!blockingOk || !identityOk || launchMutation.isPending}>
          <Rocket className="lucide-inline" /> {launchMutation.isPending ? i18nT('pages.settings.remoteCrewPanel.launching') : i18nT('pages.settings.remoteCrewPanel.launch')}
        </Btn>
        <span className="text-[12px] text-muted">
          {identityResolving
            ? identityUnknown
              ? i18nT('pages.settings.remoteCrewPanel.identity_choose')
              : i18nT('pages.settings.remoteCrewPanel.identity_resolving')
            : !identityOk
              ? i18nT('pages.settings.remoteCrewPanel.identity_incomplete')
              : blockingOk ? i18nT('pages.settings.remoteCrewPanel.ready_in_6') : i18nT('pages.settings.remoteCrewPanel.finish_prereqs')}
        </span>
      </div>
    </Card>
    </>
    ) : registeredProvisioner && selectedProvisioner ? (
      // The edition owns this form entirely — its own inputs, its own copy,
      // its own prerequisites. Isolated in an ErrorBoundary so a throwing
      // renderer costs the user this form and not the whole Settings page;
      // the progress card and the status notice below still render, which is
      // what keeps a launch already in flight visible.
      <ErrorBoundary scope={`remote-provisioner:${selectedProvisioner.kind}`}>
        <registeredProvisioner.component
          provisioner={selectedProvisioner}
          launch={input => launchMutation.mutate({
            provider_id: selectedProvisioner.id,
            profile: input.profile ?? '',
            region: input.region ?? '',
            size_key: input.size_key,
          })}
          launching={launchMutation.isPending}
          disabled={disabled}
          activeJob={activeJob}
        />
      </ErrorBoundary>
    ) : null}

  </div>
  )

  /**
   * How a launch in flight reports itself, and how a failed one reports at all.
   *
   * At PANEL level, outside both tabs and outside the crew launcher's disclosure. A
   * launch can be started from either tab now -- the crews tab's EC2 form, or the mates
   * tab's picker -- and this is the ONLY render of `job.error` and of a failed status
   * poll. Inside the launcher it was reachable only on the crews tab with that
   * disclosure open, so a mate launch whose POST succeeded and whose JOB then failed
   * (an ECS RunTask refusal, a stale `confirmed_recipient`, the engine's own
   * `mate_name_refusal`) left `launchMutation.onError` unfired, dropped out of
   * `inProgress` as its row disappeared, and said nothing anywhere.
   */
  const launchSurface = (
    <>
      {/* The progress poll itself failed. Gated on the query having a job to
          poll (`effectiveLaunchId`), NOT on `activeJob`: when the polled
          detail never arrived and the list carries no copy either, the card
          below is absent and this notice is the only thing that says why.
          When a card IS showing, it shows the last state received, not a
          live one, and would otherwise just stop moving. askAgent ON: the
          launch form persists its size and account, so the navigation loses
          nothing. */}
      {effectiveLaunchId && launchStatusQuery.isError && (
        <ErrorNotice
          className="mb-4"
          message={errMsg(launchStatusQuery.error, i18nT('pages.settings.remoteCrewPanel.launch_status_unavailable'))}
          askAgent
        />
      )}
      {activeJob && (
        <div className="mb-4">
          <LaunchProgressCard
            job={activeJob}
            cancelling={cancelMutation.isPending && cancelMutation.variables === activeJob.id}
            onCancel={id => cancelMutation.mutate(id)}
            onRestartSignin={id => signinRestartMutation.mutate(id)}
            restartingSignin={signinRestartMutation.isPending}
            onFetchSignin={id => signinMutation.mutate(id)}
            fetchingSignin={signinMutation.isPending}
            signinNotice={signinNotice && signinNotice.jobId === activeJob.id ? signinNotice : null}
          />
        </div>
      )}
    </>
  )
  const Tabs = (
    <div className="flex gap-1 border-b border-border mb-5">
      {/* Two tabs, one per THING: a crew is a gateway, a mate is one agent. Each count
          is its own list's, so the strip also says which of the two you have. */}
      <button
        type="button"
        onClick={() => setTab('crews')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.remote_crews')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'crews' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        <Server size={14} /> {i18nT('pages.settings.remoteCrewPanel.remote_crews')}
        <span className={`text-[11px] px-1.5 rounded-full ${tab === 'crews' ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>{crewRows.length}</span>
        {/* A launch in flight, on the tab it will land on. */}
        {pendingCrews.length > 0 && <span className="w-1.5 h-1.5 rounded-full bg-accent" aria-hidden />}
      </button>
      <button
        type="button"
        onClick={() => setTab('mates')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.remote_mates')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'mates' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        <Rocket size={14} /> {i18nT('pages.settings.remoteCrewPanel.remote_mates')}
        <span className={`text-[11px] px-1.5 rounded-full ${tab === 'mates' ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>{mateRows.length}</span>
        {pendingMates.length > 0 && <span className="w-1.5 h-1.5 rounded-full bg-accent" aria-hidden />}
      </button>
    </div>
  )

  const Notices = (
    <>
      {/* askAgent ON on both notices: every unsaved input this panel holds
          outlives the navigation — the add-crew and edit-crew forms live in the
          store (setCrewAddForm / setCrewEditForm), the launch form's size and
          account in localStorage — so the hand-off destroys nothing, and every
          message here (a refused connect, a failed diagnose, a rejected launch)
          is a gateway-side failure the agent can look into. */}
      {actionErr && <ErrorNotice message={actionErr} onDismiss={() => setActionErr(null)} className="mb-3" askAgent />}
      {chainRefusal && (
        // askAgent on: the reason names a limit of the arrangement (too deep, a
        // loop) rather than something to retype here, so the next step is a
        // conversation about the topology, not another press of Connect.
        <ErrorNotice
          message={chainRefusal}
          onDismiss={clearChainRefusal}
          className="mb-3"
          askAgent
          testId="remote-crew-chain-refused"
        />
      )}
      {/* A `warn` diagnosis names the broken link (`diagnosis.reason`, or the
          tunnel's own `status.error`), so it is an error surface. The structured
          `report` is passed when the journal produced one, so the hand-off carries
          the transport and stage rather than a message match. `ok` / `info`
          describe a state that has not gone wrong and stay a status note — a
          healthy "All checks passed" must not paint red or offer an agent hand-off. */}
      {diagNote?.kind === 'warn' && (
        <ErrorNotice
          message={diagNote.text}
          report={diagReport ?? undefined}
          askAgent
          onDismiss={() => { setDiagNote(null); setDiagReport(null) }}
          className="mb-3"
          testId="remote-crew-diagnosis"
        />
      )}
      {diagNote && diagNote.kind !== 'warn' && (
        <div
          role="status"
          data-testid="remote-crew-diagnosis-status"
          className={
            'mb-3 flex items-start gap-2 px-3 py-2 text-[13px] rounded-md border ' +
            (diagNote.kind === 'ok'
              ? 'bg-ok/10 text-ok border-ok/30'
              : 'bg-accent/10 text-accent border-accent/30')
          }
        >
          <Stethoscope size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{diagNote.text}</span>
          <button
            type="button"
            aria-label={i18nT('pages.settings.instancesPanel.dismiss_diagnosis')}
            className="shrink-0 opacity-70 hover:opacity-100"
            onClick={() => { setDiagNote(null); setDiagReport(null) }}
          >
            <X size={12} />
          </button>
        </div>
      )}
    </>
  )

  return (
    <div>
      {Tabs}
      {Notices}
      {launchSurface}
      {/* Mounted ONCE, outside the tab branch: the mates tab's picker and the crews
          tab's launcher both open a dialog that must survive a tab switch, and one
          living inside either branch would unmount the moment the other was used. */}
      <DeployMateDialog
        open={deployOpen}
        onClose={() => setDeployOpen(false)}
        launching={launchMutation.isPending}
        region={region}
        onLaunch={(req: DeployMateRequest) => {
          // The picker chose the lane, so `provider_id` is always named; the
          // recipient rides along only when the lane published one, and `size_key`
          // is the shared default tier — the Fargate engine maps the three
          // interactive tier keys to cpu/memory pairs itself, which is what lets
          // one picker serve both lanes without asking for a Fargate shape.
          launchMutation.mutate({
            provider_id: req.provisioner.id,
            profile,
            region,
            size_key: mateSizeKey,
            mate_name: req.mateName,
            ...(req.confirmRecipient ? { confirm_recipient: req.confirmRecipient } : {}),
          })
        }}
      />

      {tab === 'crews' ? (
        <div className="space-y-4">
          {autoConnectCard}
          {rowsCard({
            rows: crewRows,
            pending: pendingCrews,
            warmLine: true,
            heading: i18nT('pages.settings.remoteCrewPanel.crews_you_can_switch_to'),
            what: i18nT('pages.settings.remoteCrewPanel.deploy_crew_footnote'),
            empty: i18nT('pages.settings.remoteCrewPanel.no_crews'),
            action: (
              <>
                <Btn
                  primary={!showCrewLauncher}
                  onClick={() => setShowCrewLauncher(v => !v)}
                  disabled={launchMutation.isPending}
                  aria-expanded={showCrewLauncher}
                  aria-controls="crew-launcher-panel"
                  data-testid="deploy-crew-open"
                >
                  <Rocket className="lucide-inline" />{' '}
                  {showCrewLauncher
                    ? i18nT('pages.settings.remoteCrewPanel.hide_launcher')
                    : i18nT('pages.settings.remoteCrewPanel.deploy_crew_title')}
                </Btn>
                {/* Only while the launcher is CLOSED. Beside "Hide the launcher" it
                    described neither the button nor anything the reader could see. */}
                {!showCrewLauncher && (
                  <span className="text-[11px] text-muted-strong">
                    {i18nT('pages.settings.remoteCrewPanel.deploy_crew_footnote')}
                  </span>
                )}
              </>
            ),
          })}
          {/* Open only on request: an AWS probe, a size ladder, a subnet and an identity
              is a long form, and the answer to "which crews do I have" must not sit
              below all of it. */}
          {showCrewLauncher && crewLauncher}
          <AddInstanceForm onAdded={reloadInstances} />
        </div>
      ) : (
        <div className="space-y-4">
          {rowsCard({
            rows: mateRows,
            pending: pendingMates,
            heading: i18nT('pages.settings.remoteCrewPanel.mates_you_can_chat_to'),
            what: i18nT('pages.settings.remoteCrewPanel.deploy_mate_footnote'),
            empty: i18nT('pages.settings.remoteCrewPanel.no_mates'),
            action: (
              <>
                <Btn
                  primary
                  onClick={() => setDeployOpen(true)}
                  disabled={launchMutation.isPending}
                  data-testid="deploy-mate-open"
                >
                  <Rocket className="lucide-inline" />{' '}
                  {i18nT('pages.settings.remoteCrewPanel.deploy_mate_title')}
                </Btn>
                <span className="text-[11px] text-muted-strong">
                  {i18nT('pages.settings.remoteCrewPanel.deploy_mate_footnote')}
                </span>
              </>
            ),
          })}
          {/* What the mate lane IS, when one is configured: which mate its image serves
              and the region it launches into. Said before a launch rather than after a
              refused one. */}
          {mateLane && (
            <FargateLaunchCard provisioner={mateLane} region={region} sizeKey={sizeKey} />
          )}
          {/* A mate lane the gateway does not offer. Said here, because the mates tab is
              where a reader looks for it -- the picker's disabled chip says the same
              thing, and only after they open it. */}
          {!mateLane && provisionersQuery.isSuccess && (
            <Card>
              <div className="text-[13px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.lane_fargate_absent')}
              </div>
            </Card>
          )}
          {provisionersQuery.isError && (
            <ErrorNotice
              message={errMsg(provisionersQuery.error, i18nT('pages.settings.remoteCrewPanel.provisioners_unavailable'))}
              askAgent
            />
          )}
        </div>
      )}
    </div>
  )
}
