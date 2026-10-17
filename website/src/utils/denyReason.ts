/**
 * The one frontend copy of the backend's `DENY_REASON_PREFIX` (`security.py`).
 *
 * Held as a REGEX, not a string constant, and exported so `RecoveryCard` builds
 * its own end-anchored matcher from `.source` rather than declaring the literal a
 * second time. This is a WIRE VALUE matched byte-for-byte against a Python
 * constant, never copy: as a string literal inside an ALL-CAPS module constant
 * the i18n gate reads it as untranslated UI text and asks for a catalog key, and
 * translating it would silently stop every deny reason from being found.
 *
 * `test_recovery_card_prefixes.py` asserts the Python constant still appears
 * here, so the two languages cannot drift apart unnoticed.
 */
export const DENY_REASON_MARKER = /Blocked by security policy:/g

/**
 * Pull the human-readable deny reason out of a blocked tool row's content.
 *
 * When a security-policy rule or a PreToolUse hook blocks a tool call, the
 * gateway appends a second tool message sharing the pill's `tool_call_id`:
 *
 *     🚫 <title> — Blocked by security policy: <pattern>
 *     <why the pattern fired, when the match was structural>
 *
 * The Output panel used to discard that content and show a fixed
 * "blocked by security policy" line, because the row could arrive carrying only
 * a bare title: a later `tool_call_update` title refinement rewrote the row as
 * `"<icon> <title>"`, deleting the reason. With that rewrite fixed backend-side
 * the content is dependable, so the reason can be shown instead of a placeholder
 * that tells the user nothing about WHICH rule fired or why.
 *
 * Reads the LAST marker, never the first. `<title>` is model-authored —
 * `_select_tool_title` prefers the tool call's own `description` field — so a
 * model that writes "Blocked by security policy: …" into its description would,
 * under first-match extraction, get its own text rendered to the user AS the
 * security reason. The gateway always appends the real reason after the title,
 * so the final occurrence is the one the host wrote.
 *
 * Returns the marker AND everything after it, so a caller that needs the wire
 * form keeps it; `extractDenyDetail` is the variant for display beside a
 * localized lead. A row without the marker yields "" and the caller keeps its
 * placeholder.
 */
export function extractDenyReason(rowContent: string): string {
  if (!rowContent) return ''
  let last: RegExpMatchArray | null = null
  for (const m of rowContent.matchAll(DENY_REASON_MARKER)) last = m
  if (!last || last.index === undefined) return ''
  const reason = rowContent.slice(last.index).trim()
  // A marker with nothing after it is a placeholder, not a reason — let the
  // caller's own localized placeholder win rather than rendering a bare colon.
  return reason === last[0] ? '' : reason
}

/**
 * The deny reason WITHOUT the English marker — the rule that fired, plus the
 * structural note when there is one.
 *
 * Exists so the Output panel can lead with its own LOCALIZED sentence and follow
 * with the detail, instead of replacing a translated sentence with untranslated
 * English. The marker earns its place on the wire, where three parsers key on it;
 * it earns nothing in front of a reader who is already being told, in their own
 * language, that a safety policy blocked the call.
 */
export function extractDenyDetail(rowContent: string): string {
  const reason = extractDenyReason(rowContent)
  if (!reason) return ''
  return reason.replace(DENY_REASON_MARKER, '').trimStart()
}

/**
 * The gateway writes a blocked row as `🚫 <title> — <reason>`. This matches the
 * gate-crash refusal (`hooks.py`: `GATE_CRASH_REASON`) as the WHOLE tail of the
 * row, word for word, with only the exception class name free. A WIRE VALUE
 * like `DENY_REASON_MARKER`: translating it would stop the reason from being
 * found, and it must change together with `GATE_CRASH_REASON`.
 *
 * End-anchored on the full fixed sentence because `<title>` is model-authored.
 * A looser match (a separator plus "Blocked: ") lets a title such as
 * `x — Blocked: <text>` stand in for the host's reason on any deny whose own
 * reason lacks that lead word. Only the host can put this exact sentence at
 * the end of the row, since the host appends its reason after the title.
 */
const GATE_CRASH_NOTICE =
  / — (Blocked: the safety check crashed while judging this call \([A-Za-z_][\w.]*\), so the call was refused and nothing ran\. This is a Kiro Crew bug, not a policy rule and not a user action\.)$/

/**
 * The host's gate-crash refusal sentence, for a blocked row that carries NO
 * `DENY_REASON_MARKER`.
 *
 * `extractDenyDetail` yields "" for such a row, and the Output panel then falls
 * back to its localized "blocked by security policy" line alone. For the
 * gate-crash row that is exactly backwards: the reason says, in so many words,
 * that NO policy rule fired and the user did nothing. This returns that
 * sentence so the panel can render it instead of the lead. Every other row
 * yields "" and keeps the localized line alone, exactly as before.
 */
export function extractDenyNotice(rowContent: string): string {
  if (!rowContent || extractDenyReason(rowContent)) return ''
  const m = rowContent.trimEnd().match(GATE_CRASH_NOTICE)
  return m ? m[1] : ''
}
