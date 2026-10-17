/**
 * Settings -> Remote Crew: the launch-job predicates, the remembered launch-form
 * keys, the size tiers the launcher offers (kept in step with cloud/sizes.py),
 * and the sentences a sign-in failure is shown as. Pure logic, no JSX; read by
 * `RemoteCrewPanel.tsx` and its two component owners.
 */
import {
  type LaunchJob,
} from '../../api/client'
import {
  launchIsInFlight,
} from '../../utils/remoteCrew'
import { i18nT } from '../../i18n/t'

/** A launch job the user is still waiting on (not yet a switchable crew). The
 *  status list is the shared one in utils/remoteCrew, so the Members page's
 *  cloud panel and this one classify a launch the same way. */
export const isInProgress = (j: LaunchJob) => launchIsInFlight(j.status)

/** Remembered across navigation — see the state declarations for why. */
export const CLOUD_PROFILE_KEY = 'mc-cloud-profile'
export const CLOUD_REGION_KEY = 'mc-cloud-region'
// The launch form's third input. Persisted like the two above rather than held in
// the component, because every way out of this panel unmounts it — the agent
// hand-off's navigation, a sidebar click, the back button — and a size picked and
// then silently reset to the recommended default is a launch the user did not ask
// for.
export const CLOUD_SIZE_KEY = 'mc-cloud-size'
// Which provisioner the setup tab is drawing. Persisted for the same reason as
// the three fields above — every way out of this panel unmounts it, and silently
// reverting to the built-in EC2 launcher would put the user in front of a
// different form (and a different bill) than the one they chose.
export const CLOUD_PROVISIONER_KEY = 'mc-cloud-provisioner'
export const DEFAULT_REGION = 'us-east-1'

/** A launch that has reached a final state — nothing more will happen to it. */
const TERMINAL: LaunchJob['status'][] = ['done', 'failed', 'cancelled']
export const isTerminal = (j: LaunchJob) => TERMINAL.includes(j.status)

/** The connect (register) step ran — the crew exists under Your crews, so a
 *  sign-in can be re-run against it rather than provisioning anything. */
export const isRegistered = (j: LaunchJob) => j.steps.some(st => st.key === 'connect' && st.state === 'done')

/** A launch that created a crew but whose Kiro sign-in never confirmed. Such a
 *  crew is registered (Stop / Delete work) but a chat on it fails with "not
 *  logged in", so it must not read as ready to connect. */
export const needsSignin = (j: LaunchJob) =>
  isTerminal(j) && !!j.instance_id && j.signin_detected !== true && isRegistered(j)

/** Whether the Connect step's icon should read as waiting rather than done.
 *  What it answers is "is this crew signed in", NOT "is this job over" — a
 *  registered crew waiting on a code is unsigned for the whole approval, which
 *  is most of the time a user spends looking at the card. */
export const connectWaiting = (j: LaunchJob) => isRegistered(j) && j.signin_detected !== true

/** The launch chose a company (IAM Identity Center) identity, so its code is
 *  approved through the organization's portal and not the Builder ID one.
 *  A job from an older gateway carries no `login_target` at all, which means
 *  Builder ID — it must not be told it has a company account it never had. */
export const isSsoLaunch = (j: LaunchJob) => !!j.login_target?.start_url

/** What Cancel actually destroys, which is not one thing.
 *
 *  On a registered crew the click only stops the Kiro sign-in — the instance and
 *  the crew row survive, and the job goes back to done. On a launch that has not
 *  registered yet it removes the instance being created. One word for both left
 *  the reader unable to tell which, so the label names the blast radius. The
 *  accessible name carries it too: an aria-label that still said "Cancel setup"
 *  would take the distinction back away from the readers who need it most. */
export function cancelCopy(job: LaunchJob) {
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

/** Size tiers offered in the launcher, laddered by how many sub-agents run at
 *  once (CPU-bound, see cloud/sizes.py). Kept in sync with sizes.py's arm64
 *  lane; the numbers are display-only and match `SizeTier`. */
export interface SizeTier {
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
export const SIZE_TIERS: SizeTier[] = [
  { key: 'light', family: 'light', arch: 'arm64', instanceType: 't4g.xlarge', vcpu: 4, ramGb: 16, diskGb: 40, subagents: 3 },
  { key: 'balanced', family: 'balanced', arch: 'arm64', instanceType: 'm7g.2xlarge', vcpu: 8, ramGb: 32, diskGb: 60, subagents: 6, recommended: true },
  { key: 'power', family: 'power', arch: 'arm64', instanceType: 'm7g.4xlarge', vcpu: 16, ramGb: 64, diskGb: 80, subagents: 12 },
]
// The x86_64 lane, shown only when the disclosure is expanded. It exists because
// some images and toolchains are still amd64-only; the shapes mirror the arm64
// ladder so the sub-agent counts match. Keys match cloud/sizes.py's x86 lane.
export const X86_TIERS: SizeTier[] = [
  { key: 'light-x86', family: 'light', arch: 'x86_64', instanceType: 't3.xlarge', vcpu: 4, ramGb: 16, diskGb: 40, subagents: 3 },
  { key: 'balanced-x86', family: 'balanced', arch: 'x86_64', instanceType: 'm7i.2xlarge', vcpu: 8, ramGb: 32, diskGb: 60, subagents: 6 },
  { key: 'power-x86', family: 'power', arch: 'x86_64', instanceType: 'm7i.4xlarge', vcpu: 16, ramGb: 64, diskGb: 80, subagents: 12 },
]

// Full literal keys per tier so the i18n key-reference gate can verify them
// statically (a map-field indirection is opaque to it).
export const tierLabel = (key: SizeTier['family']) =>
  key === 'light'
    ? i18nT('pages.settings.remoteCrewPanel.tier_light')
    : key === 'balanced'
      ? i18nT('pages.settings.remoteCrewPanel.tier_development')
      : i18nT('pages.settings.remoteCrewPanel.tier_power')
export const tierWhy = (key: SizeTier['family']) =>
  key === 'light'
    ? i18nT('pages.settings.remoteCrewPanel.tier_light_why')
    : key === 'balanced'
      ? i18nT('pages.settings.remoteCrewPanel.tier_development_why')
      : i18nT('pages.settings.remoteCrewPanel.tier_power_why')

export const PRICING_CALCULATOR_URL = 'https://calculator.aws'
// NOTE: the session-manager-plugin install command is NOT hardcoded here. It has to
// match the platform of the machine running the gateway — which may be a Linux host
// while this dashboard is open on a Mac — so the preflight response carries it.

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
export function gatewayIsDown(detail: string): boolean {
  return /failed to fetch|networkerror|network error|load failed|err_connection|unreachable|econnrefused/.test(
    detail.toLowerCase(),
  )
}

export function failureSentence(detail: string): string | null {
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
export type SigninNotice =
  | { kind: 'not_signed_in_yet' }
  | { kind: 'request_failed'; detail: string }
