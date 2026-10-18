/**
 * Settings -> Remote Crew -> Set up a new one: the size and provisioner cards,
 * the prerequisite rows, the launch progress card, the "Setting up" row, the
 * launch cancel, and the device-code sign-in block that both the progress card
 * and a crew row render. One of the owners `RemoteCrewPanel.tsx` composes.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Rocket,
  RefreshCw,
  AlertTriangle,
  CheckCircle,
  Circle,
  Copy,
  Check,
  ExternalLink,
  Loader2,
  KeyRound,
} from 'lucide-react'
import {
  type LaunchJob,
  type RemoteProvisioner,
} from '../../api/client'
import { Card, Btn, Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import type { ErrorReport } from '../../utils/errorReport'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import {
  isTerminal, isRegistered, needsSignin, connectWaiting, isSsoLaunch, cancelCopy, type SizeTier,
  tierLabel, tierWhy, gatewayIsDown, failureSentence, type SigninNotice,
} from './remoteCrewPanelModel'

/** One selectable size card. Shared by the arm64 ladder and the x86 lane so the
 *  disclosure offers real choices rather than describing sizes it cannot select. */
export function SizeCard({ tier, on, onPick }: { tier: SizeTier; on: boolean; onPick: (k: SizeTier['key']) => void }) {
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
export function ProvisionerCard({ provisioner, on, onPick }: {
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

/** The device code the user must approve, with its actions. Shared by the setup
 *  card and the crew row so the code is reachable from BOTH tabs — a user who
 *  left the setup tab must not have to find their way back to finish. */
export function SigninPromptBlock({ job, onRestart, restarting, onFetch, fetching, notice, compact, crewName }: {
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
  crewName?: string
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
            ? crewName
              ? i18nT('pages.settings.remoteCrewPanel.code_ready_for', { name: crewName })
              : i18nT('pages.settings.remoteCrewPanel.code_ready')
            : crewName
              ? i18nT('pages.settings.remoteCrewPanel.get_a_code_for', { name: crewName })
              : i18nT('pages.settings.remoteCrewPanel.get_a_code')
          : crewName
            ? isSsoLaunch(job)
              ? i18nT('pages.settings.remoteCrewPanel.approve_your_code_sso_for', { name: crewName })
              : i18nT('pages.settings.remoteCrewPanel.approve_your_code_for', { name: crewName })
            : isSsoLaunch(job)
              ? i18nT('pages.settings.remoteCrewPanel.approve_your_code_sso')
              : i18nT('pages.settings.remoteCrewPanel.approve_your_code')}
      </div>
      <div className="text-[12px] text-muted mt-0.5">
        {starting
          ? i18nT('pages.settings.remoteCrewPanel.sign_in_starting')
          : stale
            // The "start over" sentence names the link below, so it is shown
            // only where that link is: a crew that never registered has none.
            ? canRestart
              ? `${i18nT('pages.settings.remoteCrewPanel.sign_in_unconfirmed')} ${i18nT('pages.settings.remoteCrewPanel.sign_in_unconfirmed_expired')}`
              : i18nT('pages.settings.remoteCrewPanel.sign_in_unconfirmed')
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

export function SettingUpRow({ job, onCancel, cancelling }: { job: LaunchJob; onCancel: (id: string) => void; cancelling: boolean }) {
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
      <div className="sm:shrink-0 flex items-center gap-2 flex-wrap">
        {/* Names what it removes. Only unregistered launches reach this row (a
            sign-in retry is a crew row below), so this is always the teardown
            case — but it reads the same test as the card rather than hardcoding
            the answer, so the copy cannot drift from the filter above it. */}
        <CancelLaunchBtn job={job} onCancel={onCancel} cancelling={cancelling} />
      </div>
    </div>
  )
}

/** One AWS prerequisite row (ok / warn) with optional command + actions. */
export function PrereqRow({
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
  failure,
  failureDetail,
  failureReport,
  steps,
  commandNote,
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
  /** The check's own failure diagnostic (a CLI error, a refused binary).
   *  Shown in place of `detail`, through ErrorNotice, because it is the
   *  outcome of something that failed rather than status text. */
  failure?: string
  /** The tool's own output behind `failure` (the AWS CLI's error line), when
   *  `failure` is a plain-language sentence written about it. Rendered under
   *  the sentence, smaller and monospace, so the step to take leads and the
   *  raw text reads as a detail. It stays the notice's `message`, so the agent
   *  hand-off carries the CLI's own words. */
  failureDetail?: string
  /** What the hand-off on `failure`'s notice carries, when it is more than
   *  the text on screen. */
  failureReport?: ErrorReport
  /** What the reader does about `failure`, set as plain text under its notice
   *  rather than inside the alert, so the steps do not read as part of the
   *  error. `link` is an external page that carries out a step. */
  steps?: { text: string; link?: { href: string; label: string } }
  /** Under `command`: where it runs and what it changes. */
  commandNote?: string
}) {
  return (
    <li className="flex items-start gap-3 py-2.5 border-b border-border last:border-b-0">
      <span className={`mt-0.5 w-[18px] h-[18px] shrink-0 grid place-items-center rounded-full ${ok ? 'bg-ok text-ok-fg' : 'bg-warn text-warn-fg'}`}>
        {ok ? <Check size={11} /> : <AlertTriangle size={11} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-text-strong">{title}</div>
        {failure ? (
          // askAgent ON: the launch form on this tab persists its size and
          // account, so the navigation loses nothing. showIcon OFF: the row's
          // warning circle already marks the failed check.
          <div className="mt-0.5">
            {failureDetail ? (
              <ErrorNotice
                variant="inline"
                showIcon={false}
                title={failure}
                message={failureDetail}
                messagePlacement="below"
                messageClassName="font-mono whitespace-pre-wrap"
                report={failureReport}
                askAgent
              />
            ) : (
              <ErrorNotice variant="inline" showIcon={false} messageClassName="whitespace-pre-wrap" message={failure} report={failureReport} askAgent />
            )}
            {steps ? (
              <>
                <p className="mt-1.5 text-[12px] text-text">{steps.text}</p>
                {steps.link ? (
                  <a className="mt-1 w-fit flex items-center gap-1 text-[12px] text-accent font-medium hover:underline" href={steps.link.href} target="_blank" rel="noreferrer">
                    {steps.link.label} <ExternalLink size={12} aria-hidden="true" />
                  </a>
                ) : null}
              </>
            ) : null}
          </div>
        ) : detail ? <div className="text-[12px] text-muted mt-0.5 whitespace-pre-wrap">{detail}</div> : null}
        {command ? (
          <code className="block mt-1.5 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 font-mono text-[12px] text-accent overflow-x-auto">
            {command}
          </code>
        ) : null}
        {command && commandNote ? <p className="mt-1 text-[12px] text-muted">{commandNote}</p> : null}
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
export function LaunchProgressCard({
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
