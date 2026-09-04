import { memo } from 'react'
import { ChevronRight, Info, Layers, RotateCcw, TriangleAlert } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { DENY_REASON_MARKER } from '../../utils/denyReason'
import { useRowDisclosure } from './rowDisclosure'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/**
 * The synthetic-continuation prefixes the gateway prepends when it recovers a
 * turn that ended early. Kept in sync with the constants in
 * `src/kiro_crew/dashboard/state.py` (REFUSAL_RECOVERY_PREFIX,
 * STALE_RECOVERY_PREFIX, TOOL_STALL_RECOVERY_PREFIX, CONN_RECOVERY_PREFIX,
 * BUSY_RECOVERY_PREFIX, POSTTOKEN_RECOVERY_PREFIX,
 * EMPTY_RESPONSE_RECOVERY_PREFIX, COMPACTION_RECOVERY_PREFIX,
 * HOOK_CONTINUATION_RECOVERY_PREFIX, HOOK_HALTED_RECOVERY_PREFIX,
 * SUBAGENT_SYNTHESIS_PREFIX).
 *
 * Detection is by content prefix rather than a meta flag on purpose: the rows
 * are appended with a plain CSS-class meta ("msg msg-inject"), and matching the
 * text means history-restored rows written by any gateway version render as a
 * card too.
 */
export type RecoveryKind =
  | 'refusal'
  | 'tool_blocked'
  | 'stalled'
  | 'tool_stall'
  | 'connection'
  | 'busy'
  | 'posttoken'
  | 'empty'
  | 'promise_only'
  | 'compaction'
  | 'manual'
  | 'hook'
  | 'hook_halted'
  | 'synthesis'
  /**
   * Catch-all for an `inject` row this build has no dedicated prefix for — a
   * gateway newer than the frontend, or a shape nobody has written copy for yet.
   * Never produced by {@link parseRecoveryMessage} (which returns null on an
   * unknown prefix); it is constructed at the render site so the `inject` branch
   * can be terminal instead of leaking machine prose into a chat bubble.
   */
  | 'generic'

/**
 * WIRE VALUES, never rendered — do not translate. These are matched with
 * `startsWith` against gateway-authored content and must stay byte-identical to
 * the Python constants; the matched prefix is then SLICED OFF, so no character
 * of it reaches the screen. The card's visible copy is the `i18nT()` title /
 * detail below. Translating these would silently stop every recovery card from
 * rendering in that locale.
 */
const PREFIXES: ReadonlyArray<[RecoveryKind, string]> = [
  ['refusal', '[Tool refusal — automatic recovery]'],
  // A policy block whose reason was steered into the RUNNING turn, so no
  // continuation was needed. Display-only: the row exists so the block is
  // visible as this card rather than only as a generic "Steered" chip, which
  // reads as though the person had steered the turn. Not a recovery — nothing
  // was interrupted and nothing was re-sent — so its copy says neither.
  ['tool_blocked', '[Tool blocked — reason sent to the agent]'],
  ['stalled', '[Stalled turn — automatic recovery]'],
  ['tool_stall', '[Tool stall — automatic recovery]'],
  ['connection', '[Connection lost — automatic recovery]'],
  ['busy', '[Session busy — automatic recovery]'],
  ['posttoken', '[Interrupted turn — automatic recovery]'],
  ['empty', '[Empty response — automatic recovery]'],
  ['promise_only', '[Unfinished action — automatic recovery]'],
  // The context window filled mid-turn, the backend summarized, and the turn
  // then ended without finishing the request. Distinct from `posttoken`: nothing
  // errored and nothing was lost — the earlier messages were deliberately
  // replaced by a summary — so its copy names compaction as the cause rather
  // than reporting a backend fault the user might go looking for.
  ['compaction', '[Context compacted — automatic recovery]'],
  // The only USER-initiated entry in this family. Kept here because the row is
  // the same shape (an `inject` continuation the model reads), but its copy must
  // not claim an automatic recovery — a person pressed Continue.
  ['manual', '[Continue — requested by the user]'],
  // A Stop hook returned a block decision. Also not a recovery: the turn
  // finished and a hook asked for another, so its copy names the hook as the
  // cause rather than reporting an interruption that never happened.
  ['hook', '[Hook continuation — automatic]'],
  // The nudge-cap backstop fired: a Stop-hook run hit agent.max_stop_hook_nudges
  // and was halted with no turn dispatched. Informational, not a continuation —
  // the reached depth rides after the marker as " #N".
  ['hook_halted', '[Stop-hook nudge cap reached]'],
  // Fired once after every sub-agent in a fan-out has completed and each result
  // has been processed in its own turn. Not a recovery either: nothing failed.
  // It is an orchestration prompt asking for the consolidated write-up, so its
  // copy names the fan-out rather than reporting an interruption.
  // WIRE VALUE, not copy: matched byte-for-byte against SUBAGENT_SYNTHESIS_PREFIX
  // in src/kiro_crew/dashboard/state.py and then sliced off, so no character of
  // it reaches the screen. Translating it would stop the card rendering in that
  // locale — the failure the block comment above this table describes. Exempted
  // by shape (leading bracketed ALL-CAPS tag) in eslint.i18n.config.js.
  ['synthesis', '[SYSTEM] Sub-agent synthesis:'],
]

/**
 * `Blocked by security policy: <pattern>` — the deny pattern that fired.
 *
 * Built from the single exported marker rather than re-declaring the literal:
 * this is a wire value compared byte-for-byte against `security.py`, and a second
 * copy would let one half drift while the other kept matching.
 */
const POLICY_RE = new RegExp(`${DENY_REASON_MARKER.source}\\s*(.+?)\\s*$`, 'gm')

/**
 * Deny cause → the card's always-visible detail line.
 *
 * Wire values, matched byte-for-byte against `DENY_CAUSE_*` in
 * `dashboard/state.py`; never translate the KEYS of this map. The reason the map
 * exists at all is the same one the notice's own wording is cause-specific for: an
 * invalid tool name and a faulted hook are not policy verdicts, so a single
 * "safety policy blocked the call" summary asserts a cause the system knows is
 * false and points the reader at a security rule that does not exist.
 */
const TOOL_BLOCKED_DETAIL: Record<string, string> = {
  policy: 'pages.chat.recoveryCard.safety_policy_told_in_turn',
  invalid_name: 'pages.chat.recoveryCard.invalid_name_told_in_turn',
  hook_error: 'pages.chat.recoveryCard.hook_fault_told_in_turn',
}
/** A blocked-item bullet in the refusal body (`  - <tool>: <reason>`). */
const BULLET_RE = /^\s*-\s+\S/

export interface ParsedRecovery {
  kind: RecoveryKind
  /** What happened, stated as fact. Never claims the recovery succeeded. */
  title: string
  /** Cause plus the attempt, e.g. "safety policy blocked the call · continuation sent automatically". */
  detail: string
  /** Trailing chip: the deny pattern, or a count when several distinct ones fired. */
  chip: string
  /** The verbatim injected prompt, minus the prefix line. */
  body: string
}

/**
 * Parse a recovery continuation into card fields, or null when `content` is not
 * one.
 *
 * Titles deliberately describe the EVENT, not an outcome. At the moment the row
 * renders, the continuation has only just been injected — the model may adapt,
 * or may correctly decide it cannot proceed and stop. "Recovered" would claim a
 * result that has not happened yet, so the attempt lives in `detail` instead.
 */
export function parseRecoveryMessage(content: string): ParsedRecovery | null {
  const raw = content ?? ''
  const found = PREFIXES.find(([, prefix]) => raw.startsWith(prefix))
  if (!found) return null
  const [kind, prefix] = found
  const body = raw.slice(prefix.length).trim()

  if (kind === 'stalled') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.turn_stalled'),
      detail: i18nT('pages.chat.recoveryCard.recovered_continuing'),
      chip: '',
      body,
    }
  }
  if (kind === 'tool_stall') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.tool_stopped_responding'),
      detail: i18nT('pages.chat.recoveryCard.cancelled_continuing'),
      chip: '',
      body,
    }
  }
  if (kind === 'connection' || kind === 'posttoken') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.turn_interrupted'),
      detail: i18nT('pages.chat.recoveryCard.backend_error_continuing'),
      chip: '',
      body,
    }
  }
  if (kind === 'busy') {
    // Same event as `connection` (a turn cut short by a reset) and the same routine
    // severity, but NOT the same cause: nothing errored or dropped, the backend
    // session was occupied. `detail` is documented as the cause plus the attempt and
    // every sibling names one, so it names this one too rather than reusing the
    // cause-neutral phrasing — distinguishing busy from connection is why this kind
    // exists, and the collapsed card is where that distinction has to land.
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.turn_interrupted'),
      detail: i18nT('pages.chat.recoveryCard.session_busy_continuing'),
      chip: '',
      body,
    }
  }
  if (kind === 'empty') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.no_response_returned'),
      detail: i18nT('pages.chat.recoveryCard.empty_output_continuing'),
      chip: '',
      body,
    }
  }

  if (kind === 'promise_only') {
    // The turn announced an immediate action ("I'll do that now") then yielded
    // without doing it. Title states the event; detail names the cause + the one
    // automatic continuation, matching every sibling's "cause · attempt" shape.
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.action_not_taken'),
      detail: i18nT('pages.chat.recoveryCard.announced_no_action_continuing'),
      chip: '',
      body,
    }
  }

  if (kind === 'compaction') {
    // Title states the event (the context was compacted mid-turn); detail names
    // that cause plus the one automatic continuation, matching every sibling's
    // "cause · attempt" shape.
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.context_compacted'),
      detail: i18nT('pages.chat.recoveryCard.summarized_mid_turn_continuing'),
      chip: '',
      body,
    }
  }

  if (kind === 'manual') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.continued_by_you'),
      detail: i18nT('pages.chat.recoveryCard.resuming_the_interrupted_turn'),
      chip: '',
      body,
    }
  }

  if (kind === 'hook') {
    // Its own copy rather than a reused interruption label: the turn ran to
    // completion and a Stop hook asked for another, so nothing was interrupted,
    // stalled or recovered. The hook's own instruction is the expandable body.
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.continued_by_a_hook'),
      detail: i18nT('pages.chat.recoveryCard.hook_requested_continuing'),
      chip: '',
      body,
    }
  }

  if (kind === 'hook_halted') {
    // Not a continuation: the nudge cap fired and no turn was dispatched. The
    // reached depth rides after the marker as " #N" on the marker line; surface
    // it in the chip and strip that line from the body.
    const after = raw.slice(prefix.length)
    const nl = after.indexOf('\n')
    const markerRest = (nl === -1 ? after : after.slice(0, nl)).trim()
    const depth = markerRest.match(/#(\d+)/)
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.hook_loop_halted'),
      detail: i18nT('pages.chat.recoveryCard.nudge_cap_reached'),
      chip: depth ? `#${depth[1]}` : '',
      body: nl === -1 ? '' : after.slice(nl + 1).trim(),
    }
  }

  if (kind === 'synthesis') {
    // The marker ends with a colon and the instruction continues on the SAME
    // line (unlike every sibling, whose marker is a standalone bracketed token).
    // `body` therefore already holds the instruction with no leading blank line
    // to strip — the generic slice+trim above is correct as-is.
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.subagents_finished'),
      detail: i18nT('pages.chat.recoveryCard.consolidated_summary_requested'),
      chip: '',
      body,
    }
  }

  // Refusal: count the blocked-item bullets and collect the distinct deny
  // patterns. A turn can refuse several calls, and they need not share a cause.
  const blocked = body.split('\n').filter(line => BULLET_RE.test(line)).length
  // `tool_blocked` carries its DENY CAUSE on the marker line, the way
  // `hook_halted` carries `#<depth>`. Read here rather than in that branch so the
  // generic marker slice stays the single place the first line is parsed.
  const afterMarker = raw.slice(prefix.length)
  const markerNewline = afterMarker.indexOf('\n')
  const markerCause = (
    markerNewline === -1 ? afterMarker : afterMarker.slice(0, markerNewline)
  ).trim()
  // The cause is a WIRE token (`policy`, `invalid_name`, `hook_error`), consumed
  // above to pick the summary wording. It must not also survive as the first
  // line of the expanded body, where it reads as machine noise above the host
  // notice -- `hook_halted` strips its own marker line for the same reason.
  // Rows written before the cause was added carry an empty marker line, and
  // this drops that instead, which is what the generic `.trim()` did for them.
  const blockedBody = markerNewline === -1 ? '' : afterMarker.slice(markerNewline + 1).trim()
  const patterns = new Set<string>()
  for (const m of body.matchAll(POLICY_RE)) patterns.add(m[1])
  const distinct = [...patterns]
  const chip =
    distinct.length === 1
      ? distinct[0]
      : distinct.length > 1
        ? i18nT('pages.chat.recoveryCard.n_patterns', { count: distinct.length })
        : ''
  const title =
    blocked > 1
      ? i18nT('pages.chat.recoveryCard.n_tool_calls_blocked', { count: blocked })
      : i18nT('pages.chat.recoveryCard.tool_call_blocked')
  // Same EVENT as `refusal` — the host blocked a call — so it shares the title
  // and the pattern chip. Only the second half differs: nothing was interrupted
  // and no continuation was sent, because the reason went to the agent inside
  // the turn that was already running. Saying "continuation sent" here would
  // describe a turn that never happened.
  if (kind === 'tool_blocked') {
    return {
      kind,
      title,
      // Keyed on the cause the marker line carries. A single "safety policy
      // blocked the call" summary would assert a cause the system knows is false
      // for two of the three — an invalid tool name and a faulted hook are not
      // policy verdicts — and send the reader to audit a security rule that does
      // not exist. The expandable body names the real cause either way; this is
      // the line they see WITHOUT expanding. Unknown/absent cause falls back to
      // the policy wording, matching the backend's own cause default.
      detail: i18nT(TOOL_BLOCKED_DETAIL[markerCause] ?? TOOL_BLOCKED_DETAIL.policy),
      chip,
      body: blockedBody,
    }
  }
  return {
    kind,
    title,
    detail: i18nT('pages.chat.recoveryCard.safety_policy_continuing'),
    chip,
    body,
  }
}

/**
 * Structural provenance stamped on an `inject` row's `meta` by the gateway.
 *
 * Kept in sync with the `injectKind` values written at the append sites in
 * `src/kiro_crew/dashboard/chat_runner.py`, `dashboard/handlers/messaging.py`
 * and `slack/gateway.py`.
 *
 * Why this exists rather than more prefix matching: `meta` is persisted and
 * restored, whereas an `inject` row's `cls` is NOT (chat_persistence keeps `cls`
 * only for `role === "system"`), so `meta.cronLabel` — synthesized from `cls` at
 * emit time — vanishes after a flush + rehydrate. Anything keyed on its absence
 * therefore mis-renders every restored row.
 */
export type InjectKind = 'cron' | 'recovery' | 'synthesis' | 'user_replay'

/**
 * Decide which card, if any, an `inject` row gets. The single decision point
 * shared by ChatPage and the transcript-renderer registry, so the surfaces
 * cannot disagree.
 *
 * Returns a {@link ParsedRecovery} to render as a folded note, or null to mean
 * "not mine — let the caller's own renderer draw this row".
 *
 * Null is deliberately the answer for three distinct cases, because rendering a
 * row as machine prose when it is not is strictly worse than the reverse:
 *
 *   - a CRON row, whose scheduled output is the user's own and which owns a
 *     dedicated labelled bubble downstream;
 *   - a USER_REPLAY row — `build_recovery_requeue` replays the user's original
 *     message verbatim when the turn emitted nothing, and that is speech;
 *   - an UNMARKED row, i.e. one persisted by a gateway older than this field.
 *     Those keep whatever the surface drew before, so no history changes
 *     rendering underneath the user.
 */
export function resolveInjectCard(m: { content: string; meta?: Record<string, unknown> | null }): ParsedRecovery | null {
  // Content-based first: the recovery markers live in the text, which IS durable,
  // and they carry per-kind copy no structural tag can reproduce.
  const recovery = parseRecoveryMessage(m.content ?? '')
  if (recovery) return recovery

  const meta = m.meta ?? {}
  const kind = meta.injectKind as InjectKind | undefined
  // POSITIVE allowlist, deliberately: only a row that declares itself
  // gateway-authored becomes a note. Everything else returns null, which covers
  // `cron`, `user_replay` and an unstamped legacy row in one rule — explicit
  // early returns for those three were measured to be unreachable (the allowlist
  // already rejects them), so they are omitted rather than kept as dead code.
  // The behavioural contract for each is pinned in RecoveryCard.test.tsx.
  if (kind !== 'recovery' && kind !== 'synthesis') return null

  return {
    kind: 'generic',
    title: i18nT('pages.chat.recoveryCard.system_notice'),
    detail: i18nT('pages.chat.recoveryCard.injected_by_the_gateway'),
    chip: '',
    body: m.content ?? '',
  }
}

/**
 * Compact one-line card for an automatic turn-recovery continuation.
 *
 * The injected text is machine-facing instruction ("decide how to proceed…") —
 * it belongs in the transcript for auditability but does not deserve a
 * full-width bubble every time a deny pattern fires. Collapsed it states what
 * happened and which pattern fired, which is the part the user acts on;
 * expanding reveals the prompt verbatim so nothing is hidden, only folded.
 *
 * Expansion is per-row local state and is not persisted: the transcript is
 * virtualized, so a scrolled-away row remounts collapsed. Same behaviour as
 * NudgeCard.
 */
export default memo(function RecoveryCard({ parsed, disclosureKey }: { parsed: ParsedRecovery; disclosureKey?: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const { kind, title, detail, chip, body } = parsed
  // Severity split: a refusal or a stall means something was blocked or died and
  // the user may need to act, so it keeps the warning triangle. A transient
  // backend error, an empty generation, or a continuation someone asked for is
  // not a fault the user must act on — a neutral retry glyph, so it does not
  // read as urgently as a deny-pattern block.
  const routine =
    kind === 'connection' ||
    kind === 'busy' ||
    kind === 'posttoken' ||
    kind === 'empty' ||
    kind === 'promise_only' ||
    kind === 'compaction' ||
    kind === 'manual' ||
    kind === 'hook' ||
    kind === 'synthesis' ||
    kind === 'generic'
  // Synthesis is routine, but the retry glyph would misdescribe it — nothing is
  // being retried, several results are being folded into one. Layers says that.
  // A generic notice makes no claim at all about what happened, so it gets the
  // neutral info glyph rather than borrowing another kind's meaning.
  const Icon =
    kind === 'synthesis' ? Layers : kind === 'generic' ? Info : routine ? RotateCcw : TriangleAlert

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="recovery-card"
      data-kind={kind}
      data-severity={routine ? 'routine' : 'attention'}
    >
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        // Deliberately NO aria-label: it would REPLACE the accessible name, so
        // assistive tech would announce only "Show recovery details" and never
        // the title, detail or deny pattern this card exists to surface — the
        // one thing a screen-reader user would otherwise have to expand the raw
        // machine prose to learn. The inner text names the button (matching what
        // sighted users read) and aria-expanded carries the toggle state.
        className="w-full flex items-center gap-2 px-3 py-2 min-w-0 text-left text-[13px] leading-5 hover:text-text transition-colors"
        data-testid="recovery-card-toggle"
      >
        <ChevronRight
          size={13}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <Icon
          size={13}
          className={`lucide-inline shrink-0 ${routine ? 'text-muted' : 'text-warn'}`}
          aria-hidden="true"
        />
        <span className="font-medium text-text shrink-0">{title}</span>
        <span className="truncate text-[12px] leading-5 opacity-75 min-w-0">{detail}</span>
        {chip && (
          <code
            className="ml-auto shrink-0 max-w-[45%] truncate text-[11px] leading-4 px-1.5 py-0.5 rounded border border-border bg-bg-elevated font-mono"
            data-testid="recovery-card-chip"
          >
            {chip}
          </code>
        )}
      </button>
      {expanded && (
        <div
          className="px-3 pb-3 pt-2 text-[12px] font-mono leading-5 whitespace-pre-wrap overflow-hidden border-t border-border"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="recovery-card-body"
        >
          {body}
        </div>
      )}
    </div>
  )
})
