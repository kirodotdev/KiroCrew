import { memo } from 'react'
import { ChevronRight, RotateCcw, TriangleAlert } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'

/**
 * The synthetic-continuation prefixes the gateway prepends when it recovers a
 * turn that ended early. Kept in sync with the constants in
 * `src/kiro_crew/dashboard/state.py` (REFUSAL_RECOVERY_PREFIX,
 * STALE_RECOVERY_PREFIX, TOOL_STALL_RECOVERY_PREFIX, CONN_RECOVERY_PREFIX,
 * BUSY_RECOVERY_PREFIX, POSTTOKEN_RECOVERY_PREFIX,
 * EMPTY_RESPONSE_RECOVERY_PREFIX).
 *
 * Detection is by content prefix rather than a meta flag on purpose: the rows
 * are appended with a plain CSS-class meta ("msg msg-inject"), and matching the
 * text means history-restored rows written by any gateway version render as a
 * card too.
 */
export type RecoveryKind =
  | 'refusal'
  | 'stalled'
  | 'tool_stall'
  | 'connection'
  | 'busy'
  | 'posttoken'
  | 'empty'
  | 'manual'

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
  ['stalled', '[Stalled turn — automatic recovery]'],
  ['tool_stall', '[Tool stall — automatic recovery]'],
  ['connection', '[Connection lost — automatic recovery]'],
  ['busy', '[Session busy — automatic recovery]'],
  ['posttoken', '[Interrupted turn — automatic recovery]'],
  ['empty', '[Empty response — automatic recovery]'],
  // The only USER-initiated entry in this family. Kept here because the row is
  // the same shape (an `inject` continuation the model reads), but its copy must
  // not claim an automatic recovery — a person pressed Continue.
  ['manual', '[Continue — requested by the user]'],
]

/** `Blocked by security policy: <pattern>` — the deny pattern that fired. */
const POLICY_RE = /Blocked by security policy:\s*(.+?)\s*$/gm
/** A blocked-item bullet in the refusal body (`  - <tool>: <reason>`). */
const BULLET_RE = /^\s*-\s+\S/

export interface ParsedRecovery {
  kind: RecoveryKind
  /** What happened, stated as fact. Never claims the recovery succeeded. */
  title: string
  /** Cause plus the attempt, e.g. "safety policy · continuing automatically". */
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

  if (kind === 'manual') {
    return {
      kind,
      title: i18nT('pages.chat.recoveryCard.continued_by_you'),
      detail: i18nT('pages.chat.recoveryCard.resuming_the_interrupted_turn'),
      chip: '',
      body,
    }
  }

  // Refusal: count the blocked-item bullets and collect the distinct deny
  // patterns. A turn can refuse several calls, and they need not share a cause.
  const blocked = body.split('\n').filter(line => BULLET_RE.test(line)).length
  const patterns = new Set<string>()
  for (const m of body.matchAll(POLICY_RE)) patterns.add(m[1])
  const distinct = [...patterns]
  return {
    kind,
    title:
      blocked > 1
        ? i18nT('pages.chat.recoveryCard.n_tool_calls_blocked', { count: blocked })
        : i18nT('pages.chat.recoveryCard.tool_call_blocked'),
    detail: i18nT('pages.chat.recoveryCard.safety_policy_continuing'),
    chip:
      distinct.length === 1
        ? distinct[0]
        : distinct.length > 1
          ? i18nT('pages.chat.recoveryCard.n_patterns', { count: distinct.length })
          : '',
    body,
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
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const { kind, title, detail, chip, body } = parsed
  // Severity split: a refusal or a stall means something was blocked or died and
  // the user may need to act, so it keeps the warning triangle. A transient
  // backend error or an empty generation is infrastructure noise the gateway
  // handles on its own — a neutral retry glyph, so a routine hiccup does not
  // read as urgently as a deny-pattern block.
  const routine =
    kind === 'connection' ||
    kind === 'busy' ||
    kind === 'posttoken' ||
    kind === 'empty' ||
    kind === 'manual'
  const Icon = routine ? RotateCcw : TriangleAlert

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md border border-border bg-card text-muted animate-scale-in"
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
        className="w-full flex items-center gap-1.5 px-2.5 py-1.5 min-w-0 text-left text-[13px] hover:text-fg transition-colors"
        data-testid="recovery-card-toggle"
      >
        <ChevronRight
          size={13}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <Icon
          size={13}
          className={`lucide-inline shrink-0 ${routine ? 'text-muted' : 'text-warning'}`}
          aria-hidden="true"
        />
        <span className="font-medium text-fg shrink-0">{title}</span>
        <span className="truncate text-[12px] opacity-75 min-w-0">{detail}</span>
        {chip && (
          <code
            className="ml-auto shrink-0 max-w-[45%] truncate text-[11px] px-1.5 py-0.5 rounded border border-border bg-bg-elevated font-mono"
            data-testid="recovery-card-chip"
          >
            {chip}
          </code>
        )}
      </button>
      {expanded && (
        <div
          className="px-2.5 pb-2.5 pt-2 text-[12px] font-mono leading-relaxed whitespace-pre-wrap overflow-hidden border-t border-border"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="recovery-card-body"
        >
          {body}
        </div>
      )}
    </div>
  )
})
