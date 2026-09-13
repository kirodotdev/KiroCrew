/**
 * `(recommended)` inside an option label is PROTOCOL, not label text.
 *
 * The dashboard's injected option-label rules instruct the agent to emit the
 * marker (`_OPTIONS_RECOMMENDED_RULE`, backend side), and only the dashboard
 * receives that rule. This grammar still RECOGNISES the marker rather than
 * requiring it: an unmarked label is valid and costs nothing.
 *
 * Recognising it matters because of where it lands. The label is dispatched verbatim
 * as the user's next message, so a marker left in it makes the user appear to be
 * recommending something to the assistant. It also renders as plain text inside
 * `ChipLabel`'s single clamped line, styled exactly like the instruction it sits
 * beside and competing with it for that line's width.
 *
 * Splitting it out lets the renderer place the marker OUTSIDE the clamped span,
 * where no label length can hide it, and keeps the chip one line tall.
 *
 * It also fixes what the click sends. The label doubles as the user's next
 * message, and "(recommended) Merge it now" is not a sentence the user wrote —
 * the recommendation was the agent's. Stripping it here means every surface
 * sends the instruction and only the instruction.
 */

/**
 * The marker, in the one form that has a producer: `(recommended)`.
 *
 * Deliberately narrow, and this bounds UI copy rather than just parsing. Admitting
 * a marker is what paints a badge, so an open trailing word would let
 * `(recommended strongly)` style itself as a recommendation, and an open-ended
 * `\(([^)]*)\)` would do the same for any parenthetical in a label — "(see below)",
 * "(destructive)". The badge word itself is a constant in `ChipBadge`; this grammar
 * decides only WHETHER a label carries the marker.
 *
 * Ordering variants (`(recommended first)` / `(recommended then)`) are NOT
 * admitted: nothing in this repo emits them. Re-admit the day one is observed.
 *
 * A TRAILING marker is not admitted either, on the same standard: the producer rule
 * sanctions only the leading form, so no producer emits one. Admitting it would mean
 * removing text from a label on a guess, and a drifted trailing marker instead stays
 * visible as ordinary label text -- what happened before this grammar existed.
 *
 * LEADING ONLY, and this is a correctness boundary rather than a style choice. A
 * label is dispatched verbatim as the user's next message, so anything removed
 * from its interior is a word the user did not choose to drop: an unanchored
 * pattern turns `Search for the literal (recommended) token` into `Search for
 * the literal token`, silently changing what gets sent. The marker is a prefix on
 * the instruction; anything later is part of the instruction and is left alone.
 *
 * Not global: this is used with `exec`, and a `g` flag would carry `lastIndex`
 * between calls on a module-level regex and skip every other label.
 */
const RECOMMENDED_LEADING_RE = /^\s*\(recommended\)/i

// Labels opening with one of these DISPATCH as a harness command, a prompt mention, or
// reserved provenance. Mirrored on the backend; a test fails if the two lists diverge.
const RESERVED_DISPATCH_SIGILS = ['/', '@', '!'] as const

// Dispatch protocols longer than one character, so the sigil list above cannot carry them.
// A regex literal for the same i18n reason the provenance openers below are regexes.
// Regex literals, not strings: the i18n gate fails at zero tolerance on a new untranslated
// literal inside an ALL-CAPS constant, and these openers are protocol sentinels.
const RESERVED_PROVENANCE_RES = [
  /^\[Connection lost — automatic recovery\]/,
  /^\[Context compacted — automatic recovery\]/,
  /^\[Continue — requested by the user\]/,
  /^\[Cron notification from /,
  /^\[Empty response — automatic recovery\]/,
  /^\[End of cron notification\]/,
  /^\[Hook continuation — automatic\]/,
  /^\[Interrupted turn — automatic recovery\]/,
  /^\[Monitor wake\]/,
  /^\[SYSTEM\]/,
  /^\[Session busy — automatic recovery\]/,
  /^\[Stalled turn — automatic recovery\]/,
  /^\[Stop\-hook nudge cap reached\]/,
  /^\[Subagent batch completion event\]/,
  /^\[Subagent completion event\]/,
  /^\[Tool blocked — reason sent to the agent\]/,
  /^\[Tool refusal — automatic recovery\]/,
  /^\[Tool stall — automatic recovery\]/,
  /^\[Unfinished action — automatic recovery\]/,
] as const

// The summary pass reads a turn opening with any of these as automation rather than as
// something the user said, so a label stripping into one would forge that origin.
const INJECTED_PROVENANCE_RES = [
  /^\[cron notification/i,
  /^\[subagent completion event\]/i,
  /^\[subagent batch completion event\]/i,
  /^\[auto-nudge cycle/i,
  /^\[monitor wake\]/i,
  /^\[tool refusal/i,
  /^\[tool stall/i,
  /^=== restored context/i,
  /^\[system\]/i,
] as const

// Plan chips carry no sigil, so the prefix guard cannot see them: `isPlanAction` matches
// these casefolded, and a stripped marker would promote a label into an unattended auto-run.
const RESERVED_PLAN_ACTION_RE = /^(?:go|go all|cancel)$/i

/**
 * FIRST WORD, not the whole label: the dashboard chat endpoint reads a leading
 * `stop`/`cancel`/`abort` as an orchestration stop while an escalated run is live, so
 * it fires on `stop after this stage` and not only on a bare `stop`. `\b` keeps
 * `Stopwatch the build` and `Cancellation policy review` ordinary prose.
 */
const RESERVED_STOP_WORD_RE = /^(?:stop|cancel|abort)\b/i

// Python's `strip()` also removes U+001C-U+000D and U+0085, which `trim()` keeps; `trim()` removes
// U+FEFF, which `strip()` keeps. Both edges carry the union, so neither seam is the permissive one.
const PLAN_ACTION_TRIM_RE =
  /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]+|[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]+$/g

// The guards below must classify the string the SEND path dispatches, and that path trims the
// value first, so a leading character it drops is invisible here and present in the dispatch.
// `\s` is not that set: it omits U+001C-U+001F and U+0085, which Python's `strip()` removes.
const DISPATCH_LEADING_RE =
  /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u200B-\u200D\u2000-\u200A\u2028\u2029\u202F\u205F\u2060\u3000\uFEFF]+/

export interface SplitRecommendation {
  /** The label with the marker removed — what a click sends. */
  label: string
  /** Whether the label carried the marker. A boolean, not the marker's text:
   *  the grammar admits one spelling and `ChipBadge` holds the word, so there is
   *  no second value a caller could ever read here. */
  hasMarker: boolean
}

/**
 * Split an option label into the instruction and its recommendation marker.
 *
 * Returns the label unchanged when there is no marker, so this is safe to run
 * over every option.
 */
function applyMarkerGuards(option: string): SplitRecommendation {
  const match = RECOMMENDED_LEADING_RE.exec(option)
  if (!match) return { label: option, hasMarker: false }

  // Interior whitespace is significant -- the label is dispatched verbatim, so a double
  // space in `Run printf 'a  b'` must survive. The marker is anchored to the START.
  const label = option.slice(match[0].length).replace(PLAN_ACTION_TRIM_RE, '')

  // A label that was ONLY the marker keeps its original text. A badge names no
  // action, so stripping here would render a chip the user cannot interpret and,
  // worse, send an empty message. Treating it as unmarked is the safe reading of
  // a label that carries no instruction.
  if (!label) return { label: option, hasMarker: false }

  // What a click will actually send, so every guard below tests the dispatched string.
  const dispatched = label.replace(DISPATCH_LEADING_RE, '')
  if (!dispatched) return { label: option, hasMarker: false }

  // A label that a click would DISPATCH as something other than the user's own words is left
  // exactly as it arrived.
  //
  // This is a security boundary, not a nicety. The label is sent verbatim as the user's next
  // message, and the dashboard reads THREE leading forms as more than plain text:
  //
  //   `/`  a leading-slash first word is forwarded to the harness as a command
  //        (`is_harness_slash_command`: any member of its known set — all of which begin with `/`
  //        — or, under claude_code, any leading slash at all). `(recommended) /clear` would leave
  //        here as `/clear` and erase the transcript.
  //   `@`  a message starting with `@` is run through `_resolve_prompt_mention`, which resolves
  //        `@name` to a stored prompt and substitutes its CONTENT for the message. So
  //        `(recommended) @deploy` would leave here as `@deploy` and execute that prompt instead
  //        of sending the words the user saw.
  //   `[`  a leading bracket opens a reserved provenance prefix — synthesis, cron notification,
  //        subagent completion, monitor wake, hook continuation — byte-matched with no origin check.
  //
  // In every case stripping a front marker PROMOTES inert text into something that runs, or that
  // claims an origin it does not have: the raw option begins with `(`, so no path fires first. A
  // marker is presentation; it must never decide what runs.
  //
  // Returning the ORIGINAL text — rather than a stripped label plus a suppressed badge — is what
  // makes the property provable: for any label that is or would become a command, this function is
  // a no-op, so the behaviour is identical to not having the feature at all and no dispatch path
  // exists here that did not already exist upstream. Reporting no recommendation also keeps the
  // badge off, since the caller only records a marker when one is returned.
  //
  // Prefix-only, deliberately: any of these INSIDE a label ("Run the a/b test", "ping @ 5pm",
  // "the [SYSTEM] log") is ordinary prose, and a substring test would silently drop badges from
  // unrelated labels.
  if (RESERVED_DISPATCH_SIGILS.some(sigil => dispatched.startsWith(sigil))) {
    return { label: option, hasMarker: false }
  }

  // Case-sensitive, matching the byte comparison the action router itself performs.
  if (dispatched.startsWith('action::')) {
    return { label: option, hasMarker: false }
  }

  // Case-sensitive, matching the dispatch-side byte comparison it mirrors.
  if (RESERVED_PROVENANCE_RES.some(re => re.test(dispatched))) {
    return { label: option, hasMarker: false }
  }

  // Case-insensitive, matching how the summary pass reads these same origins.
  if (INJECTED_PROVENANCE_RES.some(re => re.test(dispatched))) {
    return { label: option, hasMarker: false }
  }

  // Same promotion, no sigil to catch it: `Go All` is dispatched to the plan-action
  // endpoint, which flips the orchestrator into unattended per-stage auto-approval.
  if (RESERVED_PLAN_ACTION_RE.test(dispatched.replace(PLAN_ACTION_TRIM_RE, ''))) {
    return { label: option, hasMarker: false }
  }

  // A stripped marker must not turn a sentence into a cancellation. The stop reader takes
  // the FIRST word, so a whole-string test would miss every label that says more than it.
  if (RESERVED_STOP_WORD_RE.test(dispatched.replace(PLAN_ACTION_TRIM_RE, ''))) {
    return { label: option, hasMarker: false }
  }

  return { label, hasMarker: true }
}

/**
 * Split a marked label, naming the cases where the guard REFUSES to strip.
 *
 * The refusal itself is correct: such a label is dispatched verbatim, so the marker
 * has to stay. But it also means the chip draws raw `(recommended)` text and a click
 * sends it as the user's own words -- the pair of harms this grammar exists to
 * prevent, reappearing whenever a producer marks a label the transport must match.
 * The guard makes that safe and silent, so the drift is undiagnosable without this.
 * Reporting only; the returned value is whatever the guard decided.
 */
/**
 * Whether the guard found a marker and kept it, so the chip draws raw `(recommended)`
 * text that a click will send. Reads the guard rather than the grammar, so the view
 * cannot drift from what dispatch actually does.
 */
/**
 * Whether dispatching *text* verbatim would be read as a COMMAND rather than as words.
 *
 * Narrower than {@link dispatchWouldPromote} on purpose, and the difference is the point.
 * A `/`, `@`, `action::` or provenance opener is never something a user meant to type into
 * a menu -- it only appears because an agent put it there, so one click must not run it.
 * Plan actions (`Go`) and stop words (`cancel`) are the opposite: they are affordances the
 * product offers and the user deliberately clicks, so refusing those would delete a feature
 * rather than close a hole. Both predicates read the same tables, so a new form registers once.
 */
export function dispatchIsCommandShaped(text: string): boolean {
  const dispatched = text.replace(DISPATCH_LEADING_RE, '')
  if (!dispatched) return false
  if (RESERVED_DISPATCH_SIGILS.some(sigil => dispatched.startsWith(sigil))) return true
  // Case-sensitive, matching the byte comparison the action router itself performs.
  if (dispatched.startsWith('action::')) return true
  if (RESERVED_PROVENANCE_RES.some(re => re.test(dispatched))) return true
  if (INJECTED_PROVENANCE_RES.some(re => re.test(dispatched))) return true
  return false
}

/**
 * Whether dispatching *text* verbatim would run something rather than say something.
 *
 * The marker guard's full question: every form whose exposure by a stripped prefix would
 * promote inert text. Includes the affordance classes, because a MARKER must never decide
 * what runs even when the underlying label is one the user may legitimately click.
 */
export function dispatchWouldPromote(text: string): boolean {
  if (dispatchIsCommandShaped(text)) return true
  const trimmed = text.replace(DISPATCH_LEADING_RE, '').replace(PLAN_ACTION_TRIM_RE, '')
  if (RESERVED_PLAN_ACTION_RE.test(trimmed)) return true
  if (RESERVED_STOP_WORD_RE.test(trimmed)) return true
  return false
}

export function markerDeclined(option: string): boolean {
  return RECOMMENDED_LEADING_RE.test(option) && !applyMarkerGuards(option).hasMarker
}


export function splitRecommendation(option: string): SplitRecommendation {
  return applyMarkerGuards(option)
}

