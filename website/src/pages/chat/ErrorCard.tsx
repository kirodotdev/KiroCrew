import { memo, useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ExternalLink, KeyRound, Loader2, MessageSquarePlus, RotateCw, Settings, ShieldCheck, SlidersHorizontal } from 'lucide-react'

import { ApiError, apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
// The shared two-step restart control. Its module holds only the About panel's
// update actions (shared components and the api client), not the Settings page.
import { RestartGatewayButton } from '../settings/AboutPanelUpdateFlow'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { chatErrorDisplayText } from '../../lib/chatErrorRecovery'
import { isStopEvent } from '../../lib/stopEvent'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import type { ChatMessage } from '../../types'
import { withOriginLink } from '../../components/withOriginLink'
import { SessionTurnLimitButton } from '../../components/SessionTurnLimitMenu'
import { injectOpensTurn } from './RecoveryCard'

/** Where a capability refusal's fix lives: the member's Approvals section in
 *  the crewmate editor, or the editor itself (its Built from field picks the
 *  agent). */
export type CapabilityFix = 'capabilities' | 'member'

/** Session-start refusals the backend stamps (`meta.code`) from
 *  `agent_capabilities.prepare_member_capabilities`
 *  (`chat_runner._SESSION_START_REFUSAL_TEXT`). A retry re-runs the same check, so each row
 *  shows its own copy and fix instead of Resume. No `fix`: the fix is outside
 *  the dashboard, and the copy names it. */
const CAPABILITY_REFUSAL_COPY = {
  materialization_changed: 'pages.chat.errorCard.capabilities_changed',
  parent_identity_changed: 'pages.chat.errorCard.capabilities_parent_changed',
  foreign_private_copy: 'pages.chat.errorCard.capabilities_foreign_copy',
  project_identity_changed: 'pages.chat.errorCard.capabilities_project_changed',
  private_template_shadowed: 'pages.chat.errorCard.capabilities_template_shadowed',
}
type CapabilityRefusalCode = keyof typeof CAPABILITY_REFUSAL_COPY
const CAPABILITY_REFUSAL_FIX: Readonly<Record<CapabilityRefusalCode, CapabilityFix | undefined>> = {
  materialization_changed: 'capabilities',
  // No dashboard surface detaches a member from its parent, so the in-app fix
  // is the editor's Built from field (or putting the parent back, as the copy says).
  parent_identity_changed: 'member',
  foreign_private_copy: 'member',
  project_identity_changed: undefined,
  private_template_shadowed: undefined,
}

export const capabilityRefusal = (m: Pick<ChatMessage, 'meta'>): { code: CapabilityRefusalCode; fix?: CapabilityFix } | undefined => {
  const code = (m.meta as { code?: unknown } | undefined)?.code
  if (typeof code !== 'string' || !Object.hasOwn(CAPABILITY_REFUSAL_COPY, code)) return undefined
  const known = code as CapabilityRefusalCode
  return { code: known, fix: CAPABILITY_REFUSAL_FIX[known] }
}

/** Row kind the backend stamps on a terminal model-entitlement rejection
 *  (`chat_utils.MODEL_UNENTITLED_KIND`). Both carriers are load-bearing for the
 *  same reason as `isRetryNotice`: the live broadcast ships `kind`, a rebuilt
 *  transcript `meta.kind`. */
const MODEL_UNENTITLED_KIND = 'model_unentitled'

export const isModelUnentitled = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === MODEL_UNENTITLED_KIND || (m.meta as { kind?: string } | undefined)?.kind === MODEL_UNENTITLED_KIND

/** Row kind the backend stamps on the terminal error an `AcpAuthRequired` turn
 *  produces (`chat_utils.AUTH_REQUIRED_KIND`): the agent process reported it is
 *  not signed in. Same two carriers as above. */
const AUTH_REQUIRED_KIND = 'auth_required'

export const isAuthRequired = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === AUTH_REQUIRED_KIND || (m.meta as { kind?: string } | undefined)?.kind === AUTH_REQUIRED_KIND

/** Row kind the backend stamps on the terminal error a SPENT PLAN ALLOWANCE
 *  produces (`chat_utils.USAGE_LIMIT_KIND`): the provider refused the turn
 *  because the account's usage limit is reached. Decided from the raw frame on
 *  the backend, never from the prose here -- a copy edit or a translation moves
 *  the words, not the kind. Same two carriers as above. */
const USAGE_LIMIT_KIND = 'usage_limit'

export const isUsageLimit = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === USAGE_LIMIT_KIND || (m.meta as { kind?: string } | undefined)?.kind === USAGE_LIMIT_KIND

/** Row kind the backend stamps on the terminal error a SESSION START that never
 *  answered produces (`chat_utils.SESSION_START_FAILED_KIND`): `session/new`
 *  timed out, so the turn has no agent session at all. Decided from the
 *  exception's tag on the backend, never from the prose here -- the timeout
 *  message carries a diagnostic suffix that changes, and a translation moves
 *  the words. Same two carriers as above. */
const SESSION_START_FAILED_KIND = 'session_start_failed'

export const isSessionStartFailed = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === SESSION_START_FAILED_KIND || (m.meta as { kind?: string } | undefined)?.kind === SESSION_START_FAILED_KIND

/** Row kind the backend stamps on the error row a turn that hit its time limit
 *  leaves (`turn_dispatch.TURN_TIMEOUT_KIND`). The limit is a setting, so the
 *  row links to it beside Resume. Same two carriers as above. */
const TURN_TIMEOUT_KIND = 'turn_timeout'

export const isTurnTimeout = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === TURN_TIMEOUT_KIND || (m.meta as { kind?: string } | undefined)?.kind === TURN_TIMEOUT_KIND

/** Row kind the backend stamps on the terminal error a CONTEXT OVERFLOW
 *  produces (`chat_utils.CONTEXT_OVERFLOW_KIND`): the conversation does
 *  not fit in one request ("Input is too long"). A retry, a Resume or another
 *  model re-sends the same history, so the row offers a fresh session instead.
 *  Same two carriers as above. */
const CONTEXT_OVERFLOW_KIND = 'context_overflow'

export const isContextOverflow = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === CONTEXT_OVERFLOW_KIND || (m.meta as { kind?: string } | undefined)?.kind === CONTEXT_OVERFLOW_KIND

/** Where the fresh-session press on a `context_overflow` row stands. */
export type FreshSessionState = 'idle' | 'pending' | 'done'

/** Row kind the backend stamps on a refused app-owned agent turn
 *  (`chat_utils.APP_AGENT_REFUSED_KIND`): the app's agent is shadowed,
 *  ambiguous or not installed. A retry re-runs the same check, so the row
 *  offers no Resume. Same two carriers as above. */
const APP_AGENT_REFUSED_KIND = 'app_agent_refused'

export const isAppAgentRefused = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === APP_AGENT_REFUSED_KIND || (m.meta as { kind?: string } | undefined)?.kind === APP_AGENT_REFUSED_KIND

/** Row kind the backend stamps on the terminal error a session start leaves
 *  when kiro-cli is not installed (`chat_utils.KIRO_CLI_MISSING_KIND`).
 *  The prose names the install fix. A feature-request chat offers the
 *  repository's form on this row, since that action is an agent turn and no
 *  agent can start. Same two carriers as above. */
const KIRO_CLI_MISSING_KIND = 'kiro_cli_missing'

export const isKiroCliMissing = (m: Pick<ChatMessage, 'kind' | 'meta'>): boolean =>
  m.kind === KIRO_CLI_MISSING_KIND || (m.meta as { kind?: string } | undefined)?.kind === KIRO_CLI_MISSING_KIND

/** Consecutive tagged session-start failures at which the card stops offering
 *  Resume and the server refuses the re-run (`session_start_repeat`). Two, not
 *  one: a single timed-out start is host weather and the first Resume is the
 *  retry it deserves; the second identical failure is the signal that nothing
 *  a retry can change is wrong. Mirrors `_SESSION_START_REPEAT_REFUSAL_AT` in
 *  `src/kiro_crew/dashboard/chat_handlers.py`. */
export const SESSION_START_REPEAT_REFUSAL_AT = 2

/**
 * How many session starts in a row failed at the tail of the transcript.
 *
 * Mirrors `session_start_failure_streak` in
 * `src/kiro_crew/dashboard/chat_api/continue_turn.py` -- the two must agree, or the card
 * hides a Resume the server would honour (or offers one it refuses). Walks back
 * from the newest row counting `error` rows of the `session_start_failed` kind.
 * Rows that are not the conversation's floor are walked past -- tool rows,
 * notices, and the `recovery` inject a Resume press lands as, which resumes the
 * SAME turn and is what makes two Resume-separated failures consecutive. The
 * walk stops at the first row that IS new information: a user or assistant row
 * with content (a typed retry is a new attempt and starts the count over), a
 * Stop card, an error row of any OTHER kind (a connection-lost row is a
 * different failure, not a third start), or a row that OPENS a turn of its own
 * -- a nudge, a sub-agent completion, or an `inject` that `injectOpensTurn`
 * classifies as new work -- since a failure before such a row belongs to a
 * different turn and must not cost this turn its first Resume.
 */
export function sessionStartFailureStreak(messages: readonly ChatMessage[]): number {
  let streak = 0
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'error') {
      if (isSessionStartFailed(m)) { streak++; continue }
      break
    }
    if (isStopEvent(m)) break
    if (m.role === 'nudge' || m.role === 'subagent') break
    if (injectOpensTurn(m as { role: string; meta?: Record<string, unknown> | null })) break
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      if (m.role === 'assistant' && isSystemNoticeKind(m.kind ?? (m.meta as { kind?: string } | undefined)?.kind)) continue
      break
    }
  }
  return streak
}

/**
 * WIRE SHAPES, never rendered — the gateway's own English error prose, matched
 * byte-for-byte against `chat_runner.py` (`_emit_error` / `_emit_stale` /
 * `_emit_stall` and the `slot.append("error", …)` sites). On a row that offers
 * Resume, a match is replaced by the catalog copy beside it, so the banner
 * speaks the same verb as the button and translates with the rest of the card.
 * Anything that does not match renders verbatim, so an unknown or newer gateway
 * string still reaches the screen.
 *
 * Only "Connection lost" carries a detail — the process exit code, ` (exit N)` —
 * captured and re-interpolated so the diagnostic survives the swap.
 *
 * Keyed by wire id with literal catalog keys so `check-i18n-keys.mjs` can
 * resolve every `i18nT` target statically.
 */
const RETRY_PROSE = {
  connection_lost: {
    key: 'pages.chat.errorCard.retry_connection_lost',
    wire: /^⟳ Connection lost( \(exit -?\d+\))? — please retry\.$/,
  },
  session_busy: { key: 'pages.chat.errorCard.retry_session_busy', wire: /^⟳ Session busy — please retry\.$/ },
  turn_stalled: { key: 'pages.chat.errorCard.retry_turn_stalled', wire: /^⟳ Turn stalled — please retry\.$/ },
  tool_stalled: { key: 'pages.chat.errorCard.retry_tool_stalled', wire: /^⟳ Tool appeared stalled — please retry\.$/ },
  backend_hiccup: { key: 'pages.chat.errorCard.retry_backend_hiccup', wire: /^⟳ Backend hiccup — please retry\.$/ },
} as const

/**
 * Localised, Resume-worded copy for a known gateway retry row, or `null` for
 * anything else. Call it ONLY for a row that renders the Resume button: on a
 * row with no control (a settled or historical error, or a surface with no turn
 * to resume) the wire text must stand, because "resume to pick up where it
 * stopped" beside nothing sends the reader looking for a button that is not
 * there.
 */
export function retryProse(content: string): string | null {
  for (const { key, wire } of Object.values(RETRY_PROSE)) {
    const m = wire.exec(content)
    if (m) return i18nT(key, { detail: m[1] ?? '' })
  }
  return null
}

export interface ErrorCardProps {
  /**
   * Server- or client-authored error prose; typed diagnostic prefixes are
   * display-only. Rendered verbatim, except that on a row offering Resume a
   * known gateway "please retry" string is shown as its localised Resume-worded
   * equivalent (see {@link retryProse}).
   */
  content: string
  meta?: ChatMessage['meta']
  /**
   * True on a `model_unentitled` row rendered by a surface that cannot offer
   * one or both fix actions (a pane has no picker; an embed or popout has no
   * settings route). The prose still names "the model picker" and "Settings →
   * Chat", so the card says where those live instead of leaving the reader
   * with an instruction and nothing to press.
   */
  unentitledElsewhere?: boolean
  /**
   * Continue handler. Passed ONLY for the newest error row of a slot whose last
   * turn ended without a reply — a historical error further up the transcript is
   * settled and must not offer to resume anything.
   */
  onContinue?: () => void
  /** True while a continue request is in flight, so the press cannot double-fire. */
  continuing?: boolean
  /**
   * The fix affordances for a model-entitlement rejection. When set, the card
   * offers them INSTEAD of Continue: the backend has said retrying cannot help,
   * so a resume button on this row would only replay the same rejection.
   * `onPickModel` opens the session's model picker; `onOpenDefaultModel` deep
   * links to Settings → Chat → Default Model, the value every new session
   * inherits and the one that keeps re-creating this error until it changes.
   */
  onPickModel?: () => void
  onOpenDefaultModel?: () => void
  /**
   * The fix affordance for an `auth_required` row: deep link to the Kiro
   * sign-in card in Settings, where the user signs in to Kiro Crew's own
   * identity again. Offered INSTEAD of Continue for the same reason as the
   * entitlement actions -- a retry hits the same signed-out wall -- and on
   * EVERY such row, because a lapsed sign-in is settled state the user still
   * has to act on. Omitted on a surface with no settings route (embed, popout).
   */
  onOpenSignIn?: () => void
  /**
   * True on the newest `session_start_failed` row when the same session start
   * has already failed `SESSION_START_REPEAT_REFUSAL_AT` times in a row with
   * nothing but Resume presses between the attempts. The host withholds
   * `onContinue` for that row (the server refuses the re-run too, with
   * `session_start_repeat`), and this flag makes the card say WHY there is no
   * Resume and what does end it: restarting the gateway. Without it the row
   * would be a bare red line where the button used to be, and the loop from
   * the field report -- Resume, same 90 s wall, Resume -- would simply become
   * a dead end with no next step on it.
   */
  sessionStartRepeat?: boolean
  /**
   * The in-product remedy for a `sessionStartRepeat` row: restart the gateway
   * (`POST /api/restart`) through the shared two-step `RestartGatewayButton`.
   * Omitted on a surface that cannot drive the gateway (embed, popout); the
   * card then keeps the terminal hint, with a copy control on its command chip.
   *
   * Only the server's 200 means the restart was accepted: any rejection (the
   * endpoint's own refusal of a restart that cannot run, or a gateway that
   * never answered) is shown as a failure, never as a restart under way.
   */
  onRestartGateway?: () => Promise<unknown>
  /**
   * The non-inference exit for a `usage_limit` row in a slot the header's
   * "Request a Feature" action created (#13342): the repo's feature-request
   * issue form. That action is an agent turn by design, so a spent allowance
   * refuses it -- and this row was where the request dead-ended, at the one
   * moment the user had no inference left. Offered INSTEAD of Continue, which
   * would replay the rejection; the backend's own sentence (which limit, the
   * request id) stays. The host passes it only for that slot: a usage limit in
   * an ordinary chat has no form to offer and keeps today's card.
   */
  featureRequestFormUrl?: string
  /**
   * Set beside `featureRequestFormUrl` when the row is a `kiro_cli_missing`
   * start failure rather than a spent allowance: the same form is offered, and
   * only the hint and the link label change, since the usage-limit copy (no
   * monthly usage spent) does not describe a host with no kiro-cli.
   */
  kiroCliMissing?: boolean
  /**
   * The fix affordance for a capability refusal row (`capabilityRefusal`):
   * open where this member's fix lives. Offered INSTEAD of Continue: a retry
   * repeats the same check. Omitted on a surface with no crew editor route
   * (embed, popout) and for a code whose fix is outside the dashboard.
   */
  onOpenCapabilities?: () => void
  /**
   * The fix affordance for a `turn_timeout` row: open Settings → Chat →
   * Advanced on the Turn Time Limit field, the setting that decides how long
   * a turn may run. Offered BESIDE Resume, not instead of it: resuming is
   * still the way to finish this turn, and the setting is how the next long
   * one avoids the cut. Omitted on a surface with no settings route.
   */
  onOpenTurnLimit?: () => void
  /**
   * The other fix for a `turn_timeout` row: the session whose turn stopped.
   * With it, the card's limit control becomes a menu offering that session's
   * own turn time limit, with the Settings link (every session) inside it.
   */
  turnLimitSlot?: string
  /**
   * The fix for the newest `context_overflow` row: drop this slot's native
   * conversation so the next message starts a fresh one, seeded from a text
   * recap of the transcript (images left out). Offered INSTEAD of Resume, which
   * would re-send the history that overflowed. Omitted on a settled row.
   */
  onFreshSession?: () => void
  /** Where that press stands: `pending` while the request runs, `done` once the
   *  session was reset (the card then says to send the message again). */
  freshSession?: FreshSessionState
}

// No `shrink-0` and no `truncate`: every action sits in a flex row, and a
// label longer than the card (the feature-request form's, which names its
// destination and its cost, runs 44 chars in English and 62 in German) must
// break into lines inside the card rather than run past its edge. The control
// shrinks to its longest word and `text-balance` splits the label into two
// even lines instead of a long line and a stray word. The icon beside the
// label keeps its own `shrink-0`, so only the text gives.
const ACTION_BTN =
  'inline-flex items-center gap-2 text-[12px] leading-5 font-medium text-balance px-3 py-1 rounded-md border-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

/** The restart hint with its command as a `<code>` chip. The command is
 *  interpolated verbatim (never translated) inside the `i18nT` call, and the
 *  chip's position is found by rendering the same key once more with a
 *  sentinel in the placeholder's place, so the chip lands wherever the
 *  translation put `{{command}}`; a catalog that dropped the placeholder
 *  renders the sentence unchanged rather than nothing. The two `i18nT` calls
 *  are the only place the command text lives in this module. */
// Literal keys so `check-i18n-keys.mjs` resolves them statically: the terminal
// hint for whoever reads the card; the refused variant, addressed to the
// person who runs the gateway (a refused viewer cannot run the command); and
// the retry variant beside a button that can still work.
const RESTART_HINT_KEYS = {
  terminal: 'pages.chat.errorCard.session_start_repeat_hint',
  refused: 'pages.chat.errorCard.restart_gateway_refused_hint',
  retry: 'pages.chat.errorCard.restart_gateway_retry_hint',
} as const

function restartHint(variant: keyof typeof RESTART_HINT_KEYS = 'terminal'): ReactNode {
  const key = RESTART_HINT_KEYS[variant]
  // The retry line quotes the button by its own catalog label, so the words
  // always match what the shared RestartGatewayButton actually shows.
  const button = i18nT('pages.settings.aboutPanel.restart_gateway')
  const text = i18nT(key, { command: 'kirocrew restart', button })
  const marked = i18nT(key, { command: '\u0000', button })
  const at = marked.indexOf('\u0000')
  if (at < 0) return text
  const tail = marked.length - at - 1
  const command = text.slice(at, text.length - tail)
  if (!command) return text
  return (
    <>
      {text.slice(0, at)}
      <code className="font-mono text-[12px] px-1 py-0.5 rounded bg-bg-elevated ring-1 ring-inset ring-border" data-testid="error-card-restart-command">
        {command}
      </code>
      {/* The shared command copy control (SettingRef's): it paints a tick only
          when the clipboard took the text, and a cross when it did not. */}
      <span className="inline-flex align-middle ml-1" data-testid="error-card-restart-command-copy">
        <CopyCommandButton text={command} />
      </span>
      {text.slice(text.length - tail)}
    </>
  )
}

/**
 * The repeat-start card on a surface that can drive the gateway. The restart
 * is the shared `RestartGatewayButton` (Settings → About and the speech
 * settings use the same one), so the press is the same two-step arm
 * everywhere: a restart interrupts everything running on the gateway, so one
 * stray click must not do it.
 *
 * Phases: offered → restarting (button gone, so a second press cannot queue a
 * second restart); or failed. A real restart always pushes a `restarting`
 * progress step, which arms the update reload latch, so the page reloads on
 * reconnect and that reload ends the flow, as it does for Settings → About.
 *
 * A `useMutation`: while the browser is offline the mutation waits paused
 * instead of failing, so the card never claims a restart it has not sent.
 * Only the server's 200 counts as accepted: the endpoint refuses a restart
 * that cannot run (a broken launcher or interpreter) before it answers, so
 * any rejection is a failure, shown through `ErrorNotice` with no agent
 * hand-off.
 */
/** `POST /api/restart`'s refusal codes for a restart that cannot run on this
 *  install (`_resolve_restart_target`): pressing again meets the same refusal. */
const CANNOT_RUN_CODES: ReadonlySet<string> = new Set(['restart_launcher_invalid', 'restart_interpreter_missing'])

function RestartGatewayCard({ displayText, onRestartGateway }: {
  displayText: string
  onRestartGateway: () => Promise<unknown>
}) {
  const [restarting, setRestarting] = useState(false)
  // The failure, and the one next step it leaves. `owner`: the owner gate
  // refused this viewer, so only whoever runs the gateway can act. `terminal`:
  // the endpoint refused a restart that cannot run here (its 409 codes for a
  // broken launcher or interpreter), so pressing again fails the same way. Both drop
  // the button. `retry`: anything else, where a second press can work.
  const [failure, setFailure] = useState<{ title: string; detail: string; next: 'owner' | 'terminal' | 'retry' } | null>(null)
  const restart = useMutation({
    mutationFn: onRestartGateway,
    onMutate: () => setFailure(null),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // The owner gate's 403 only. A lapsed dashboard cookie also answers 403
      // (`authRequired`), but the client refreshes that in the background, so
      // the next press can pass and the button must stay.
      const refused = e instanceof ApiError && e.status === 403 && !e.authRequired
      const cannotRun = CANNOT_RUN_CODES.has(apiErrorCode(e))
      setFailure({
        title: refused ? i18nT('pages.chat.errorCard.restart_gateway_refused_title') : i18nT('pages.chat.errorCard.restart_gateway_failed'),
        detail: e instanceof ApiError ? e.message : '',
        next: refused ? 'owner' : cannotRun ? 'terminal' : 'retry',
      })
    },
  })
  const button = (
    <div className="flex flex-wrap items-center gap-2">
      <RestartGatewayButton
        primary
        pending={restart.isPending}
        restarting={false}
        onConfirm={() => restart.mutate()}
        testId="error-card-restart-gateway"
      />
    </div>
  )
  let body: ReactNode
  if (restarting) {
    body = (
      <div className="text-[13px] leading-5 text-text flex items-center gap-1.5" role="status" data-testid="error-card-restarting">
        <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
        {i18nT('pages.chat.errorCard.restart_gateway_restarting')}
      </div>
    )
  } else if (failure) {
    // The plain-words title leads; the server's own sentence (the diagnostic)
    // follows below it. One next step under it, never two that disagree: a
    // refused viewer gets the line for whoever runs the gateway, a restart
    // that cannot run here gets the terminal, anyone else gets "try again, or
    // use a terminal" beside the button. With no server
    // sentence (a network error) the title alone is the message, said once.
    const hasDetail = Boolean(failure.detail) && failure.detail !== failure.title
    body = (
      <>
        <ErrorNotice
          title={hasDetail ? failure.title : undefined}
          message={hasDetail ? failure.detail : failure.title}
          messagePlacement="below"
          variant="inline"
          noHandoffReason="This card exists because this chat's agent session cannot start; an Ask-the-agent chat would need the same start and fail the same way."
          testId="error-card-restart-failed"
        />
        {failure.next === 'owner' ? (
          <div className="text-[13px] leading-5 text-text" data-testid="error-card-restart-refused-hint">
            {restartHint('refused')}
          </div>
        ) : failure.next === 'terminal' ? (
          <div className="text-[13px] leading-5 text-text" data-testid="error-card-session-start-repeat-hint">
            {restartHint('terminal')}
          </div>
        ) : (
          <>
            {/* The cost line stays wherever the button does: a retry is the
                same disruptive restart, so it must be decided with the same
                warning in view. */}
            <div className="text-[13px] leading-5 text-text" data-testid="error-card-session-start-restart-hint">
              {i18nT('pages.chat.errorCard.session_start_repeat_restart_hint')}
            </div>
            <div className="text-[13px] leading-5 text-text" data-testid="error-card-restart-retry-hint">
              {restartHint('retry')}
            </div>
            {button}
          </>
        )}
      </>
    )
  } else {
    body = (
      <>
        <div className="text-[13px] leading-5 text-text" data-testid="error-card-session-start-restart-hint">
          {i18nT('pages.chat.errorCard.session_start_repeat_restart_hint')}
        </div>
        {button}
      </>
    )
  }
  return (
    <div
      className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
      data-testid="error-card"
      data-session-start-repeat="true"
    >
      {/* Muted once the restart is accepted: the failure it reports is being
          fixed, and red text above the restarting line reads as still broken. */}
      <div className={`${restarting ? 'text-muted' : 'text-danger'} text-[13px] leading-5 min-w-0`} style={{ overflowWrap: 'anywhere' }} data-testid="error-card-session-start-prose">
        {withOriginLink(displayText)}
      </div>
      {body}
    </div>
  )
}
/**
 * The error row in a chat transcript.
 *
 * When the turn is genuinely resumable the card carries a Resume action beside
 * the prose, and the gateway's own "please retry" wording is swapped for
 * catalog copy that names the same verb ({@link retryProse}), so the banner
 * never says "retry" next to a control that says "Resume". A row with no
 * Resume control keeps the gateway text as written.
 *
 * The button is deliberately absent rather than disabled when the turn is not
 * resumable — a permanently greyed control on a red card reads as a broken
 * feature, and there is no state the user could reach that would enable it.
 *
 * A model-entitlement rejection is the one error whose fix is NOT a retry, so
 * its row swaps Resume for the two actions that actually end it: pick a model
 * the account is served, and change the default the next session would start on.
 */
export const ErrorCard = memo(function ErrorCard({
  content: wireContent,
  meta,
  onContinue,
  continuing,
  onPickModel,
  onOpenDefaultModel,
  onOpenSignIn,
  unentitledElsewhere,
  featureRequestFormUrl,
  kiroCliMissing,
  sessionStartRepeat,
  onRestartGateway,
  onOpenCapabilities,
  onOpenTurnLimit,
  turnLimitSlot,
  onFreshSession,
  freshSession = 'idle',
}: ErrorCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Swap the gateway's "please retry" wording ONLY on a row that renders the
  // Resume button. A row with no control keeps the wire text: telling the
  // reader to resume beside nothing is worse than the mismatch it would fix.
  const content = (onContinue && retryProse(wireContent)) || wireContent
  if (featureRequestFormUrl) {
    // A feature request the plan could not afford: the one action that still
    // ends it is the tracker's own form, which needs no agent turn. The prose
    // (the provider's sentence, request id included) stays first, the one-line
    // explanation says why a form and not a retry, and the link is styled as
    // the row's primary action so it reads as the way forward rather than a
    // footnote. A plain anchor, like Report a Problem's issue link: the desktop
    // shell routes `_blank` to the system browser, and `noopener noreferrer`
    // hands the new tab no handle back to this window.
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
        data-usage-limit-fallback="true"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {content}
        </div>
        <div className="text-[12px] leading-5 text-muted" data-testid="error-card-feature-request-hint">
          {i18nT(kiroCliMissing ? 'pages.chat.errorCard.feature_request_form_cli_missing_hint' : 'pages.chat.errorCard.feature_request_form_hint')}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a
            href={featureRequestFormUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover no-underline`}
            data-testid="error-card-feature-request-form"
          >
            <ExternalLink size={12} className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT(kiroCliMissing ? 'pages.chat.errorCard.feature_request_form_cli_missing' : 'pages.chat.errorCard.feature_request_form')}
          </a>
        </div>
      </div>
    )
  }
  const refusal = capabilityRefusal({ meta })
  if (refusal) {
    // The localized copy replaces the backend's English row. `file` is the
    // basename a `private_template_shadowed` row carries, `project` the project
    // it lives in, or the saved project a `project_identity_changed` row names
    // (empty: none was saved).
    const refusalMeta = meta as { file?: unknown; project?: unknown; member?: unknown } | undefined
    const refusalVars = {
      file: String(refusalMeta?.file ?? ''),
      project: String(refusalMeta?.project ?? ''),
      name: String(refusalMeta?.member ?? ''),
      field: i18nT('pages.kiroCrewAgentsPage.built_from'),
    }
    const FixIcon = refusal.fix === 'member' ? Settings : ShieldCheck
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
        data-capabilities-changed="true"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {refusal.code === 'project_identity_changed' && !refusalVars.project
            ? i18nT('pages.chat.errorCard.capabilities_project_changed_other', refusalVars)
            : refusal.code === 'foreign_private_copy' && !refusalVars.name
              ? i18nT('pages.chat.errorCard.capabilities_foreign_copy_unnamed', refusalVars)
              : i18nT(CAPABILITY_REFUSAL_COPY[refusal.code], refusalVars)}
        </div>
        {onOpenCapabilities && refusal.fix && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onOpenCapabilities}
              className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
              data-testid="error-card-open-capabilities"
            >
              <FixIcon size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {refusal.fix === 'member'
                ? i18nT('pages.chat.errorCard.open_crewmate', { name: refusalVars.name })
                : i18nT('pages.chat.errorCard.open_capabilities')}
            </button>
          </div>
        )}
      </div>
    )
  }
  if (onOpenSignIn) {
    // A signed-out agent process: the one action that ends it is signing in
    // again from Settings. The prose (the backend's own wording, which may
    // still mention `kiro-cli login` for a kiro-cli-owned process) stays; the
    // button is the in-product path for the Crew-owned one.
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
        data-auth-required="true"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {withOriginLink(content)}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onOpenSignIn}
            className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
            title={i18nT('pages.chat.errorCard.sign_in_hint')}
            data-testid="error-card-sign-in"
          >
            <KeyRound size={12} className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT('pages.chat.errorCard.sign_in')}
          </button>
        </div>
      </div>
    )
  }
  const displayText = chatErrorDisplayText(content, meta)
  if (onFreshSession) {
    // The conversation is too big for the model: the fix is a fresh session
    // that carries a text recap of this chat. The prose (the backend's own
    // sentence) stays first; the hint says what the press keeps and drops, so
    // "fresh" does not read as "lose everything".
    // Once the reset landed the failure is over: the card drops its danger
    // colours so it reads as recovered, and the ready line becomes the body.
    const done = freshSession === 'done'
    const pending = freshSession === 'pending'
    return (
      <div
        className={`${done ? 'bg-bg-elevated ring-border' : 'bg-danger-subtle ring-danger/20'} ring-1 ring-inset forced-colors:border rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in`}
        data-testid="error-card"
        data-context-overflow="true"
        data-fresh-session={freshSession}
      >
        {/* The error sentence ends by telling the reader to start a fresh
            session, so once that is done it would ask for a step already
            taken: the recovered card shows only the ready line. */}
        {!done && (
          <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
            {withOriginLink(displayText)}
          </div>
        )}
        <div className={`text-[12px] leading-5 ${done ? 'text-text font-medium' : 'text-muted'}`} data-testid="error-card-fresh-session-hint" aria-live="polite">
          {done ? i18nT('pages.chat.errorCard.fresh_session_done') : i18nT('pages.chat.errorCard.fresh_session_hint')}
        </div>
        {!done && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onFreshSession}
              disabled={pending}
              className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
              data-testid="error-card-fresh-session"
            >
              {pending
                ? <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
                : <MessageSquarePlus size={12} className="lucide-inline shrink-0" aria-hidden="true" />}
              {i18nT('pages.chat.errorCard.fresh_session')}
            </button>
          </div>
        )}
      </div>
    )
  }
  const unentitledActions = onPickModel || onOpenDefaultModel
  // Name only the affordance THIS surface lacks: a pane has neither, an
  // embed/popout has the picker but not the settings route. Saying "the
  // picker is elsewhere" beside a live picker button misleads.
  const elsewhereKey = !unentitledElsewhere
    ? null
    : !onPickModel && !onOpenDefaultModel
      ? 'pages.chat.errorCard.elsewhere_hint'
      : onPickModel && !onOpenDefaultModel
        ? 'pages.chat.errorCard.elsewhere_settings_hint'
        : null
  const elsewhere = elsewhereKey !== null
  if (unentitledActions) {
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {withOriginLink(displayText)}
        </div>
        {onPickModel && onOpenDefaultModel && (
          // Both actions are needed, and a primary/secondary pair reads as
          // pick-one. Say the dependency on the card itself, not in a tooltip,
          // and quote the two button labels so the pair cannot read as the
          // same action twice.
          <div className="text-[12px] leading-5 text-muted" data-testid="error-card-both-hint">
            {i18nT('pages.chat.errorCard.both_hint', {
              pick: i18nT('pages.chat.errorCard.pick_model'),
              default: i18nT('pages.chat.errorCard.default_model'),
            })}
          </div>
        )}
        {elsewhere && (
          <div className="text-[12px] leading-5 text-muted" data-testid="error-card-elsewhere-hint">
            {i18nT(elsewhereKey!)}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {onPickModel && (
            <button
              type="button"
              onClick={onPickModel}
              className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
              title={i18nT('pages.chat.errorCard.pick_model_hint')}
              data-testid="error-card-pick-model"
            >
              <SlidersHorizontal size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {i18nT('pages.chat.errorCard.pick_model')}
            </button>
          )}
          {onOpenDefaultModel && (
            <button
              type="button"
              onClick={onOpenDefaultModel}
              className={`${ACTION_BTN} bg-transparent text-text ring-1 ring-inset ring-border hover:bg-bg-elevated`}
              title={i18nT('pages.chat.errorCard.default_model_hint')}
              data-testid="error-card-default-model"
            >
              <Settings size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {i18nT('pages.chat.errorCard.default_model')}
            </button>
          )}
        </div>
      </div>
    )
  }
  const resumeButton = onContinue && (
    <button
      type="button"
      onClick={onContinue}
      disabled={continuing}
      className={`${ACTION_BTN} bg-accent text-accent-fg hover:bg-accent-hover`}
      title={i18nT('pages.chat.errorCard.resume_hint')}
      data-testid="error-card-continue"
    >
      {continuing
        ? <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
        : <RotateCw size={12} className="lucide-inline shrink-0" aria-hidden="true" />}
      {i18nT('pages.chat.errorCard.resume')}
    </button>
  )
  if (onOpenTurnLimit || turnLimitSlot) {
    const secondaryBtn = `${ACTION_BTN} bg-transparent text-text ring-1 ring-inset ring-border hover:bg-bg-elevated`
    return (
      <div
        className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex flex-col gap-2 animate-scale-in"
        data-testid="error-card"
        data-turn-timeout="true"
        data-continuable={onContinue ? 'true' : undefined}
      >
        <div className="text-danger text-[13px] leading-5 min-w-0" style={{ overflowWrap: 'anywhere' }}>
          {withOriginLink(displayText)}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {resumeButton}
          {/* One limit control beside Resume: with a known session it is a
              menu holding this session's limit and the Settings link;
              without one, the Settings link alone. */}
          {turnLimitSlot ? (
            <SessionTurnLimitButton slotKey={turnLimitSlot} className={secondaryBtn} onOpenSettings={onOpenTurnLimit} />
          ) : (
            <button
              type="button"
              onClick={onOpenTurnLimit}
              className={secondaryBtn}
              title={i18nT('pages.chat.errorCard.turn_limit_hint')}
              data-testid="error-card-turn-limit"
            >
              <Settings size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              {i18nT('pages.chat.errorCard.turn_limit')}
            </button>
          )}
        </div>
      </div>
    )
  }
  if (sessionStartRepeat && onRestartGateway && !onContinue) {
    // The same start failed twice and this surface can drive the gateway: the
    // remedy is a press on the card, not a trip to a terminal. The backend's
    // own sentence (with its diagnostic) stays first.
    return (
      <RestartGatewayCard
        displayText={displayText}
        onRestartGateway={onRestartGateway}
      />
    )
  }
  if (!onContinue) {
    return (
      <div
        className="bg-danger-subtle text-danger-text text-[13px] leading-5 px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-danger/15 self-center animate-scale-in"
        data-testid="error-card"
        style={{ overflowWrap: 'anywhere' }}
      >
        {withOriginLink(displayText)}
        {elsewhere && (
          <div className="text-[12px] leading-5 text-muted mt-1" data-testid="error-card-elsewhere-hint">
            {i18nT(elsewhereKey!)}
          </div>
        )}
        {sessionStartRepeat && (
          // The same start failed twice; a third Resume would only fail the
          // same way, so the button is gone and this line is the card's ONLY
          // remaining next step. It therefore renders at body weight in the
          // card's own colour, not as a muted footnote, and the command is a
          // code chip with a copy control. This branch is the fallback for a
          // surface with no restart action (embed, popout): the terminal is
          // the next step there.
          <div className="text-[13px] leading-5 mt-1" data-testid="error-card-session-start-repeat-hint">
            {restartHint()}
          </div>
        )}
      </div>
    )
  }
  return (
    <div
      className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex items-center gap-3 animate-scale-in"
      data-testid="error-card"
      data-continuable="true"
    >
      <div className="text-danger text-[13px] leading-5 flex-1 min-w-0" style={{ overflowWrap: 'anywhere' }}>
        {withOriginLink(displayText)}
      </div>
      {resumeButton}
    </div>
  )
})
