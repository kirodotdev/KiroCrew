/**
 * The About panel's update ACTIONS, each a self-contained control: the arm-and-approve
 * step-up a managed-venv gateway runs (`InAppUpdateFlow`), the request card an agent's
 * update ask raises on a packaged desktop install (`AgentUpdateRequestCard`), the
 * two-step `RestartGatewayButton`, and `copyCommand` plus the arm timings they share.
 * `AboutPanel.tsx` re-exports the five of these it exported before
 * (`AGENT_REQUEST_POLL_MS`, `AgentUpdateRequestCard`, `InAppUpdateFlow`,
 * `RestartGatewayButton`, `resolveUnarmedPhase`), so their importers keep that path.
 */
import { useEffect, useRef, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { RefreshCw, AlertCircle, GitBranch, ArrowUp, X, Download, Copy } from 'lucide-react'
import { Btn } from '../../components/ui'
import { useAppDispatch, useAppSelector } from '../../store'
import { setUpdateProgress } from '../../store/dashboardSlice'
import { api, ApiError } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import { compareVersions } from '../../utils/releaseVersion'

/**
 * How long a primed Restart button stays armed.
 *
 * Long enough to read the changed label and click again, short enough that an
 * armed control does not sit there as a trap for an unrelated click a minute
 * later.
 */
const ARM_TIMEOUT_MS = 5000

/**
 * Consecutive `armStatus` poll failures before the armed panel says so. One or
 * two misses are the normal cost of a 5s poll on a flaky link and the local
 * countdown covers them; a run of them means the approval landing can no
 * longer be observed, which the user must hear rather than wait on forever.
 */
export const ARM_POLL_FAILURE_THRESHOLD = 3
/**
 * How often the About panel asks the gateway whether an agent has requested an
 * update. Exported for tests, which shrink it to land several consecutive
 * failures inside a test budget without touching the failure threshold.
 */
export const AGENT_REQUEST_POLL_MS = { value: 5000 }

/**
 * Copy a command and report the OUTCOME. `copyToClipboard` resolves `false`
 * when the legacy fallback fails without throwing (plain-HTTP remote gateway —
 * exactly the deployment these commands target) and rejects when both paths
 * are dead; either way painting "Copied" would tell the user their shell paste
 * is ready when the clipboard still holds something else.
 */
export async function copyCommand(
  text: string,
  setCopied: (v: boolean) => void,
  setFailed: (v: boolean) => void,
): Promise<void> {
  let ok = false
  try {
    ok = await copyToClipboard(text)
  } catch {
    ok = false
  }
  setCopied(ok)
  setFailed(!ok)
}

/**
 * Restart control with a two-step confirm.
 *
 * A restart drops every active session, so it must not fire on a single stray
 * click — but it is also recoverable (the gateway comes straight back), which is
 * why this is an armed button rather than the typed-token treatment reserved for
 * bulk-destructive actions.
 *
 * The arm auto-expires: an armed control left on screen becomes a trap for the
 * next click minutes later, when the user has forgotten what they armed. The
 * timer is cleared on unmount so a restart that navigates away cannot fire
 * `setState` on a dead component.
 *
 * Rendered at two call sites with the same behaviour and different emphasis, so
 * the confirm step cannot drift between them.
 */
export function RestartGatewayButton({
  primary,
  pending,
  restarting,
  onConfirm,
  testId,
}: {
  primary?: boolean
  pending: boolean
  restarting: boolean
  onConfirm: () => void
  testId: string
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const timer = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS)
    return () => window.clearTimeout(timer)
  }, [armed])

  const busy = pending || restarting
  return (
    <Btn
      primary={primary && !armed}
      className={armed ? '!bg-[var(--warn)] !text-[var(--warn-fg)] hover:!opacity-80' : undefined}
      disabled={busy}
      data-testid={testId}
      // No static aria-label: the visible text IS the accessible name, and it is
      // what changes to announce the armed step. Pinning the label to "Restart
      // gateway" made a screen reader say the same thing on both clicks, hiding
      // the confirm from exactly the users who cannot see the colour change.
      onClick={() => {
        if (busy) return
        if (!armed) { setArmed(true); return }
        setArmed(false)
        onConfirm()
      }}
    >
      <RefreshCw size={13} className={`lucide-inline ${busy ? 'animate-spin' : ''}`} /> {restarting
        ? i18nT('pages.settings.aboutPanel.restarting')
        : armed
          ? i18nT('pages.settings.aboutPanel.restart_confirm')
          : i18nT('pages.settings.aboutPanel.restart_gateway')}
    </Btn>
  )
}

/** The in-app update flow for a managed-venv install (RFC OQ7 step-up).
 *
 * Arming records the request server-side and yields the ONE command that can
 * approve it — run on the gateway host, where reading the nonce file proves
 * an identity a dashboard session does not have. The nonce itself never
 * reaches this client, so this panel can request an update but can never
 * approve one.
 *
 * The phase machine is what keeps the post-approval story visible: a consumed
 * request (the poll answering `armed: false`) is the APPROVAL landing, so the
 * panel narrates `applying` from the shared update-progress push instead of
 * silently reverting to the Update button; a `failed` push pins the failure
 * with a retry. Expiry is decided only by the local countdown reaching zero,
 * so a poll racing the consume can never misread an approval as an expiry.
 */
/**
 * Decide what an ARMED panel's poll answer of `armed: false` means. The wire
 * shape is ambiguous: a consumed request (approval landed) and a server-side
 * TTL lapse both read `armed: false`. A throttled background tab misses its
 * 1s countdown ticks, so the decremented counter alone would misread the
 * tab's own expiry as an approval and show "applying" forever — the decision
 * compares the absolute wall-clock deadline instead.
 */
export function resolveUnarmedPhase(deadlineMs: number, now: number): 'expired' | 'applying' {
  return now >= deadlineMs ? 'expired' : 'applying'
}

/**
 * The structured `code` of a 409 arm refusal, or null for anything else.
 *
 * Read off the `code`, not the message: the message is human copy that gets
 * reworded, and matching it would silently stop working the next time it is. The
 * body is the raw response, so a non-JSON body (a proxy error page) must not throw
 * here — an unreadable body simply carries no code. Any code is returned; which
 * ones mean the offer is gone is staleArmRefusalText's call.
 */
function armRefusalCode(e: ApiError): string | null {
  if (e.status !== 409) return null
  try {
    const code = (JSON.parse(e.body) as { code?: unknown })?.code
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

/**
 * The user-facing line for a refusal code that says the offer this arm was made
 * against is gone, or null for any other code. `arm_no_longer_offered`: nothing
 * newer is available. `arm_offer_withdrawn`: the clicked upgrade was withdrawn and
 * the lane now sits below the running build, so "already up to date" would be
 * contradicted by the channel-move offer the next status frame shows.
 *
 * A `switch` with a literal key per arm, so the i18n key gate can resolve every key.
 */
function staleArmRefusalText(code: string | null): string | null {
  switch (code) {
    case 'arm_no_longer_offered': return i18nT('pages.settings.aboutPanel.arm_no_longer_offered')
    case 'arm_offer_withdrawn': return i18nT('pages.settings.aboutPanel.arm_offer_withdrawn')
    default: return null
  }
}

export function InAppUpdateFlow({
  version,
  manualCommand,
  isChannelMove,
  onHandoff,
  askAgent = true,
}: {
  /**
   * DISPLAY ONLY — the label on the Arm button. `armUpdate()` sends no version
   * (the gateway arms against its own cached raw `latest_version`), so this is
   * safe to pass folded and MUST be: the raw candidate of a promoted stable
   * release reads `0.4.1rc1`, and a button offering to "update to 0.4.1rc1" on
   * the stable channel names a prerelease that the user did not choose.
   */
  version: string
  manualCommand: string
  /**
   * True when the target is the followed lane's release rather than a newer
   * build — i.e. this arm performs a channel MOVE, which is a downgrade by
   * construction (`channel_move_pending` is only true when the running build is
   * newer). Labelling that "Update to v0.4.1" while running v0.5.0rc3 is the
   * same direction-blind copy this change exists to remove.
   */
  isChannelMove?: boolean
  /** Close an overlay host after Ask Agent successfully navigates to chat. */
  onHandoff?: () => void
  /** Disable hand-off where an enforcement overlay must remain visible. */
  askAgent?: boolean
}) {
  const [phase, setPhase] = useState<'idle' | 'armed' | 'applying' | 'failed' | 'expired'>('idle')
  const [armed, setArmed] = useState<{
    approveCommand: string
    /**
     * What the gateway ACTUALLY armed, folded for display. Not the same as the
     * `version` prop: the gateway re-checks the feed before arming, so a panel
     * whose verdict is hours old can offer v1 and arm the newer v2. The armed
     * copy names this one, or the approval installs a version the UI never
     * mentioned. Empty for a gateway that predates the field.
     */
    versionDisplay: string
    /**
     * The version the button offered WHEN CLICKED. Captured rather than read
     * from the `version` prop: the arm's own re-check refreshes the cached
     * verdict, the next status frame moves the prop to the armed version, and a
     * note comparing against the live prop would vanish while it is being read.
     */
    offered: string
    expiresIn: number
    // Absolute wall-clock deadline. The decremented counter is display-only:
    // a throttled background tab fires the 1s tick rarely, so the counter can
    // sit far above zero long after the server TTL lapsed — every expiry
    // DECISION compares against this deadline instead.
    deadlineMs: number
  } | null>(null)
  // Mirror of `armed` for effects that must READ it without depending on it
  // (see the poll-decision effect below).
  const armedRef = useRef(armed)
  useEffect(() => {
    armedRef.current = armed
  }, [armed])
  // The armed version differs from the one the button offered, so the panel owes
  // the user an explanation rather than a bare second number. Declared once
  // because it decides BOTH armed lines: the explanatory note renders on it, and
  // the plain "This installs v…" line renders on its negation — the note's own copy
  // already opens by naming the armed version.
  //
  // Only an UPGRADE to a newer build gets the "a newer version was found" note:
  // the re-check can also arm an older build than the button named (a pulled
  // release, with an older one still ahead of the running build), and that note
  // would then be false. Such an arm renders the plain line, which names what
  // installs and claims nothing about why. A channel move's note is
  // direction-neutral, so it renders on any difference.
  const armedVersionChanged = Boolean(
    armed?.versionDisplay && armed.offered && armed.versionDisplay !== armed.offered
      && (isChannelMove || compareVersions(armed.versionDisplay, armed.offered) === 1),
  )
  const [cmdCopied, setCmdCopied] = useState(false)
  const [cmdCopyFailed, setCmdCopyFailed] = useState(false)
  const [armError, setArmError] = useState('')
  // The gateway refused to arm because the offer this panel is showing is gone.
  // Local, not derived from the status push: the refusal is a FRESHER fact than the
  // frame that produced the offer, and waiting for the push to catch up is exactly
  // the window where the panel contradicts itself. Reset when a new version is
  // offered (below) so one refusal cannot latch the control away for the session.
  const [offerVoid, setOfferVoid] = useState(false)
  // A NEW offer also retires an `arm_no_longer_offered` notice: it said nothing is
  // available, and left up it would sit above the new offer's button contradicting
  // it. Every other notice stays until the next click. `arm_offer_withdrawn`'s copy
  // announces exactly that next offer ("…which appears here"), and any other
  // failure (`arm_failed`, `arm_below_min_version`, …) can follow the arm's own
  // re-check moving the offer, so retiring it would erase why the click failed.
  const noticeRetiredByNextOffer = useRef(false)
  useEffect(() => {
    setOfferVoid(false)
    if (noticeRetiredByNextOffer.current) setArmError('')
    noticeRetiredByNextOffer.current = false
  }, [version])
  // The gateway's apply narrates over the shared update-progress push; render
  // it inline so the armed copy's "progress appears here" is literally true.
  const progress = useAppSelector(st => st.dashboard.updateProgress)
  const dispatch = useAppDispatch()
  const arm = useMutation({
    // The offered version rides as the mutation variable so `onSuccess` records
    // what THIS click was made against (see `armed.offered`).
    mutationFn: (_offered: string) => api.armUpdate(),
    onSuccess: (res, offered) => {
      if (res.armed && res.approve_command) {
        setArmError('')
        // A fresh arm starts a fresh narrative: clear any progress left by a
        // PRIOR attempt. Without this, a stale `failed` push instantly
        // bounces the new armed panel back to the failure screen, making
        // "Try again" a dead loop until some new push overwrites it.
        dispatch(setUpdateProgress(null))
        const expiresIn = res.expires_in ?? 600
        setArmed({
          approveCommand: res.approve_command,
          versionDisplay: res.version_display || '',
          offered,
          expiresIn,
          deadlineMs: Date.now() + expiresIn * 1000,
        })
        setPhase('armed')
      } else {
        setArmError(res.error || i18nT('pages.settings.aboutPanel.update_failed'))
      }
    },
    onError: (e: unknown) => {
      // The withdrawn-offer refusal is a fact the panel can state in the user's
      // language; every other refusal carries the gateway's own detail.
      const code = e instanceof ApiError ? armRefusalCode(e) : null
      const staleText = staleArmRefusalText(code)
      noticeRetiredByNextOffer.current = code === 'arm_no_longer_offered'
      setArmError(
        staleText
          ?? (e instanceof ApiError ? e.message : i18nT('pages.settings.aboutPanel.update_failed')),
      )
      // The refusal is itself proof the offer is void: the gateway sends this code
      // only when its own verdict says nothing is available. So stop presenting the
      // offer immediately (see the button below for why it must go) rather than
      // leaving it up until a later status frame happens to unmount it.
      if (staleText) setOfferVoid(true)
      // NOT ALSO refetching the status here, though that would clear the page
      // heading and the "Update available" badge in the same beat. Refetching
      // (`api.status()` + `dispatch(sseStatus(...))`) makes the gateway's own verdict
      // arrive early (it already says nothing is available — that is why this
      // refusal fired), the whole offer unmounts, and the refusal NOTICE unmounts
      // with it, because the notice lives inside the offer. The user's click would
      // then remove the button and explain nothing, which is the report-as-a-bug
      // failure; a heading that lags one status frame is not. Fixing both at once
      // means hoisting the explanation above the offer's mount condition, which is
      // a page-level change rather than a panel one.
    },
  })
  // Countdown + liveness poll while ARMED. The count is cosmetic (the server
  // enforces the TTL); the poll is what notices the request being consumed —
  // approval happens in a terminal this tab cannot see.
  const isArmed = phase === 'armed'
  useEffect(() => {
    if (!isArmed) return
    const tick = setInterval(() => {
      setArmed(a => {
        if (!a) return a
        // Derive the remaining time from the absolute deadline so a
        // throttled tab that missed ticks recovers the true remainder.
        const left = Math.ceil((a.deadlineMs - Date.now()) / 1000)
        if (left > 0) return { ...a, expiresIn: left }
        setPhase('expired')
        return a
      })
    }, 1000)
    return () => clearInterval(tick)
  }, [isArmed])
  // Liveness poll through react-query, enabled only while armed: a consumed
  // request (armed: false) is the approval landing. A single error is left to
  // retry on the next interval — the local countdown keeps running regardless —
  // but a run of ARM_POLL_FAILURE_THRESHOLD consecutive failures is surfaced
  // in the armed panel, because past that point the approval can land unseen.
  const armStatusQuery = useQuery({
    queryKey: ['update-arm-status'],
    queryFn: () => api.armStatus(),
    enabled: isArmed,
    refetchInterval: 5000,
  })
  const polled = armStatusQuery.data
  useEffect(() => {
    if (!isArmed || !polled) return
    if (!polled.armed) {
      // See resolveUnarmedPhase: consumed and expired are indistinguishable
      // on the wire, so the absolute deadline decides. Read the armed state
      // through a ref rather than an effect dependency -- an `armed`
      // dependency plus the re-anchor branch below (which builds a fresh
      // object every poll) would re-run this effect off its own write,
      // looping the render.
      const a = armedRef.current
      setPhase(a ? resolveUnarmedPhase(a.deadlineMs, Date.now()) : 'applying')
    } else if (typeof polled.expires_in === 'number') {
      const expiresIn = polled.expires_in
      // The server is authoritative for the TTL: re-anchor the deadline --
      // but only when the remainder actually moved, so a same-second poll
      // answer does not mint a fresh object and re-trigger consumers.
      setArmed(a =>
        a && a.expiresIn !== expiresIn
          ? { ...a, expiresIn, deadlineMs: Date.now() + expiresIn * 1000 }
          : a
      )
    }
  }, [isArmed, polled])
  // The apply's OUTCOME arrives on the progress push, not the poll.
  const progressStep = progress?.step
  useEffect(() => {
    if (phase !== 'applying' && phase !== 'armed') return
    if (progressStep === 'failed' || progressStep === 'error') setPhase('failed')
  }, [progressStep, phase])
  const resetFailure = () => {
    setPhase('idle')
    setArmed(null)
    setCmdCopied(false)
    setCmdCopyFailed(false)
  }
  const runAction = () => {
    if (phase === 'armed' && armed) {
      void copyCommand(armed.approveCommand, setCmdCopied, setCmdCopyFailed)
      return
    }
    if (phase === 'failed') {
      resetFailure()
      return
    }
    if (phase !== 'applying') arm.mutate(version)
  }
  const rootTestId = phase === 'armed'
    ? 'in-app-update-armed'
    : phase === 'applying'
      ? 'in-app-update-applying'
      : phase === 'failed'
        ? 'in-app-update-failed'
        : 'in-app-update'
  const canStart = phase === 'idle' || phase === 'expired'

  return (
    <div className="flex flex-col gap-2" data-testid={rootTestId}>
      {phase === 'applying' && (
        <>
          {progress?.detail && (
            <p className="text-[12px] text-muted font-mono break-all" data-testid="apply-progress">
              {progress.detail}
            </p>
          )}
          <p className="text-[12px] text-muted">
            {i18nT('pages.settings.aboutPanel.applying_restart_note')}
          </p>
        </>
      )}

      {phase === 'failed' && (
        <ErrorNotice
          message={progress?.detail || i18nT('pages.settings.aboutPanel.update_failed')}
          askAgent={askAgent}
          onHandoff={onHandoff}
        />
      )}

      {canStart && (
        <>
          {phase === 'expired' && (
            <p className="text-[12px] text-muted" data-testid="arm-expired-note">
              {i18nT('pages.settings.aboutPanel.approval_window_expired')}
            </p>
          )}
          {/* Once the gateway has said this offer no longer exists, the intro and the
              button (below) are both describing something that cannot happen, so both
              stand down and the notice is the only claim left. What this component
              CANNOT reach is the "Update available" badge in the header and the
              heading above this panel: those are rendered from the status push, so
              they clear on its next frame rather than instantly. */}
          {!offerVoid && (
            <p className="text-[13px] text-muted">
              {i18nT(isChannelMove
                ? 'pages.settings.aboutPanel.in_app_channel_move_intro'
                : 'pages.settings.aboutPanel.in_app_update_intro')}
            </p>
          )}
          {/* askAgent on: arming persists nothing client-side. The button is still
              there for a retry EXCEPT on a void offer, where retrying is the one
              thing that cannot help — hence the manual command below, which is the
              honest way out of that state. */}
          <ErrorNotice message={armError} askAgent={askAgent} onHandoff={onHandoff} testId="arm-error" />
          {manualCommand && (
            <details className="text-[12px] text-muted">
              <summary className="cursor-pointer">{i18nT('pages.settings.aboutPanel.or_update_manually')}</summary>
              <div className="mt-2 p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all">
                {manualCommand}
              </div>
            </details>
          )}
        </>
      )}

      {phase === 'armed' && armed && (
        <>
          {/* The two lines below are mutually exclusive BY CONSTRUCTION, off one
              boolean, because the note's own copy opens by naming the armed version
              ("This installs v0.4.8…"). Rendering both would say the same number
              twice, and keeping two independent conditions in sync is how they drift
              into saying it twice again later.
              Note this is NOT `armed.versionDisplay === version`: when nothing was
              offered (`version` empty) there is no difference to explain, so the note
              stays away AND the plain line has to render — that is the one case where
              suppressing it would leave the panel naming no version at all. */}
          {/* WHAT was armed, not what the button offered. The gateway re-checks the
              feed as it arms, so on a panel whose verdict has gone stale these
              differ — and the approval below installs this one.
              DECLARATIVE, not the button's own `update_to_version` / `switch_to_version`
              copy: reusing that copy would make the identical words a control in one
              state and inert text in the next, which reads ambiguously to a screen
              reader user. These open the same way
              the changed-version note does, so the armed panel speaks one vocabulary
              whether or not the number moved. */}
          {armed.versionDisplay && !armedVersionChanged && (
            <p className="text-[13px] text-text" data-testid="armed-version">
              {i18nT(isChannelMove
                ? 'pages.settings.aboutPanel.armed_switches_to_version'
                : 'pages.settings.aboutPanel.armed_installs_version', { version: armed.versionDisplay })}
            </p>
          )}
          {/* The number CHANGING is the whole point of the re-check, and it is the
              one thing the plain "This installs v…" line cannot say. Without this, the
              user reads "Update to v0.4.7" on the button and "This installs v0.4.8" here and
              has to guess whether these are two updates or one of them is wrong —
              seconds before running an approval command on another machine. Worth the
              new string: only the difference needs explaining, so it is rendered only
              when there is one, and it REPLACES the plain line rather than joining it.
              BRANCHED on the lane move, because a channel move is a downgrade by
              construction (see the `isChannelMove` prop): telling that user "a newer
              version was found just now" would be false every time, about a number
              that just went DOWN, seconds before they run a host command. The move
              variant says what actually happened — the chosen channel serves this one
              now — which is true whichever direction the version went. */}
          {armedVersionChanged && (
            <p className="text-[13px] text-text" data-testid="armed-version-changed">
              {i18nT(isChannelMove
                ? 'pages.settings.aboutPanel.armed_version_changed_channel_move'
                : 'pages.settings.aboutPanel.armed_version_changed',
              { version: armed.versionDisplay, offered: armed.offered })}
            </p>
          )}
          <p className="text-[13px] text-muted">
            {i18nT('pages.settings.aboutPanel.armed_run_on_host')}
          </p>
          <div className="p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all"
            data-testid="approve-command">
            {armed.approveCommand}
          </div>
          <span className="text-[12px] text-muted" data-testid="arm-countdown">
            {i18nT('pages.settings.aboutPanel.armed_expires_in', {
              time: `${Math.floor(armed.expiresIn / 60)}:${String(armed.expiresIn % 60).padStart(2, '0')}`,
            })}
          </span>
          {cmdCopyFailed && (
            <ErrorNotice
              variant="inline"
              message={i18nT('pages.settings.aboutPanel.copy_failed_select_the_command_and_copy_it_manually')}
              askAgent={askAgent}
              onHandoff={onHandoff}
              testId="arm-copy-error"
            />
          )}
          {armStatusQuery.failureCount >= ARM_POLL_FAILURE_THRESHOLD && (
            <ErrorNotice
              variant="inline"
              message={i18nT('pages.settings.aboutPanel.arm_status_poll_failing')}
              askAgent={askAgent}
              onHandoff={onHandoff}
              testId="arm-poll-error"
            />
          )}
          <p className="text-[12px] text-muted">
            {i18nT('pages.settings.aboutPanel.armed_waiting_note')}
          </p>
        </>
      )}

      {/* "You're already up to date" under a live "Update to v0.4.7" button is two
          contradictory statements on one screen, and the button is the dangerous
          half: pressing it can only fail the same way again. So a void offer takes
          the action away with the intro; the manual command above stays as the
          way out of that state. */}
      {!(canStart && offerVoid) && (
      <div>
        <Btn
          key="update-action"
          data-testid="in-app-update-action"
          primary={canStart}
          onClick={runAction}
          disabled={arm.isPending || phase === 'applying'}
        >
          {phase === 'applying' ? (
            <><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.applying_update')}</>
          ) : phase === 'armed' ? (
            <><Copy size={13} className="lucide-inline" /> {cmdCopied
              ? i18nT('pages.settings.aboutPanel.copied')
              : i18nT('pages.settings.aboutPanel.copy_command')}</>
          ) : phase === 'failed' ? (
            i18nT('pages.settings.aboutPanel.try_again')
          ) : arm.isPending ? (
            /* Arming re-checks the feed before it answers, so this click can wait on
               a network round trip. Disabled alone reads as a dead button, so say
               what the wait is for -- the same spinner+label pair the status line
               and the Download button already use. */
            <><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.checking_for_updates')}</>
          ) : (
            <>{isChannelMove
              ? <GitBranch size={13} className="lucide-inline" />
              : <ArrowUp size={13} className="lucide-inline" />} {version
              ? i18nT(isChannelMove
                ? 'pages.settings.aboutPanel.switch_to_version'
                : 'pages.settings.aboutPanel.update_to_version', { version })
              : i18nT('pages.settings.aboutPanel.update_now')}</>
          )}
        </Btn>
      </div>
      )}
    </div>
  )
}

/** An agent's request that this packaged desktop install be updated (issue #503).
 *
 * The gateway holds a REQUEST — version, who asked, when — and nothing else:
 * no nonce, no token, no endpoint that turns the request into an install. The
 * only control that installs is the click below, which drives the same
 * `update:download` → `update:install` IPC the ordinary update card drives.
 * So the human click IS the approval, made by a person who is present, and an
 * agent that arms a request (and can read it — it is not secret) gains exactly
 * the ability to have this card appear. That is what makes agent
 * self-approval impossible by construction rather than by fence.
 *
 * Rendered only on a packaged install (`managed_by: "electron"` on the arm
 * projection) and only while a request is live. Two answers, side by side: a
 * prompt that offers only the affirmative control for the whole TTL is
 * pressure, not consent, and declining is the cheaper mistake — the agent can
 * ask again and nothing was installed.
 */
export function AgentUpdateRequestCard({
  request,
  foundVersion,
  busy,
  panelError,
  onInstall,
  onDeclined,
}: {
  request: { request_id?: string; version?: string; requested_by?: string; armed_at?: number; expires_in?: number }
  /** What the app's updater actually found, if a check has run. */
  foundVersion?: string
  busy: boolean
  /**
   * A failure the PANEL owns and the card must show: the request poll failing
   * repeatedly, or the download the card started being refused. The card's own
   * Decline failure is tracked inside.
   */
  panelError?: string
  onInstall: () => void
  onDeclined: () => void
}) {
  const [error, setError] = useState('')
  const decline = useMutation({
    // Bound to the request this card RENDERED: a stale click must not remove a
    // newer request the user has not seen.
    mutationFn: () => api.dismissUpdateArm(request.request_id || ''),
    onSuccess: () => { setError(''); onDeclined() },
    onError: () => setError(i18nT('pages.settings.aboutPanel.agent_request_decline_failed')),
  })
  // Anchor a deadline on each poll answer and tick locally, so the countdown
  // moves every second instead of jumping with the 5s poll.
  const serverExpires = typeof request.expires_in === 'number' ? request.expires_in : null
  const [deadlineMs, setDeadlineMs] = useState<number | null>(null)
  useEffect(() => {
    setDeadlineMs(serverExpires === null ? null : Date.now() + serverExpires * 1000)
  }, [serverExpires])
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    if (deadlineMs === null) return
    const tick = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [deadlineMs])
  const expiresIn = deadlineMs === null ? null : Math.max(0, Math.floor((deadlineMs - nowMs) / 1000))
  const requested = request.version || ''
  // The requester named a version the feed does not offer. Still the human's
  // call — the card installs what the feed HAS — but say so, or the click
  // would appear to install one thing and install another.
  const differs = !!requested && !!foundVersion && requested !== foundVersion
  const askedAt = typeof request.armed_at === 'number' ? new Date(request.armed_at * 1000) : null
  // The TTL is a day, so a minutes:seconds counter would read "1439:58".
  // Humanize from the deadline instead — "in 24 hours", "in 3 minutes" — and
  // let the same deadline drive the 1 s re-render below.
  const expiresAt = deadlineMs === null ? null : new Date(deadlineMs)
  return (
    <div className="p-3 bg-bg rounded-lg border border-border flex flex-col gap-2" data-testid="agent-update-request">
      <span className="text-[13px] font-medium text-text flex items-center gap-1.5">
        <AlertCircle size={13} className="lucide-inline text-warn" />
        {i18nT('pages.settings.aboutPanel.agent_request_heading')}
      </span>
      <span className="text-[12px] text-muted" data-testid="agent-update-request-body">
        {requested
          ? i18nT('pages.settings.aboutPanel.agent_request_body_version', {
            who: request.requested_by || i18nT('pages.settings.aboutPanel.agent_request_unknown_requester'),
            version: requested,
          })
          : i18nT('pages.settings.aboutPanel.agent_request_body_latest', {
            who: request.requested_by || i18nT('pages.settings.aboutPanel.agent_request_unknown_requester'),
          })}
        {askedAt && !isNaN(askedAt.getTime()) ? ` · ${fmtRelative(askedAt)}` : ''}
      </span>
      {panelError && (
        <ErrorNotice message={panelError} askAgent testId="agent-update-request-panel-error" />
      )}
      {differs && (
        <span className="text-[12px] text-muted" data-testid="agent-update-request-differs">
          {i18nT('pages.settings.aboutPanel.agent_request_feed_differs', { requested, found: foundVersion })}
        </span>
      )}
      {expiresAt !== null && expiresIn !== null && (
        <span className="text-[12px] text-muted" data-testid="agent-update-request-countdown">
          {i18nT('pages.settings.aboutPanel.agent_request_expires', {
            when: fmtRelative(expiresAt, { now: nowMs, style: 'long' }),
          })}
        </span>
      )}
      <ErrorNotice message={error} askAgent testId="agent-update-request-error" />
      <div className="flex items-center gap-2">
        {/* The button names the version it will actually deliver. When the feed
            offers something other than what was requested, "Install & restart"
            alone leaves the one click this card exists for unanswerable —
            the reader cannot tell which version they are saying yes to. */}
        <Btn primary data-testid="agent-update-request-install" disabled={busy || decline.isPending} onClick={onInstall}>
          {busy
            ? <RefreshCw size={13} className="lucide-inline animate-spin" />
            : <Download size={13} className="lucide-inline" />}{' '}
          {foundVersion
            ? i18nT('pages.settings.aboutPanel.agent_request_install_version', { version: foundVersion })
            : i18nT('pages.settings.aboutPanel.agent_request_install')}
        </Btn>
        <Btn data-testid="agent-update-request-decline" disabled={busy || decline.isPending} onClick={() => decline.mutate()}>
          <X size={13} className="lucide-inline" />{' '}
          {i18nT('pages.settings.aboutPanel.agent_request_decline')}
        </Btn>
      </div>
    </div>
  )
}
