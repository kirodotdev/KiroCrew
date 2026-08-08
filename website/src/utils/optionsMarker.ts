// Canonical [OPTION(S):] follow-up-pill marker regex — the single source of truth
// for the frontend, mirroring the backend's ReDoS-hardened OPTIONS_RE_LINE
// (src/kiro_crew/constants.py). Import this instead of hand-rolling a copy so the
// grammar can't drift between the dashboard's several parsers.
//
// The tempered body `(?:[^[\n]|\[(?!OPTIONS?:))*` matches any run of characters that
// does NOT begin a fresh `[OPTION(S):` marker, which gives three properties:
//   1. a label may itself contain `]` — the block ends at the LAST `]` that ends the
//      line, not the first `]` (so "[OPTIONS: a] | b]]" → ["a]", "b]"]);
//   2. two same-line markers can't merge into one garbage label;
//   3. it fails in O(1) per `[OPTIONS:` prefix instead of rescanning the line, so
//      untrusted model output with thousands of `[OPTIONS:` prefixes can't drive
//      quadratic (ReDoS-class) backtracking in the synchronous render path.
// The marker must END ITS LINE (`\][ \t]*$` with the `m` flag) — a trailing note,
// question, or diff on later lines is left intact. `i` = case-insensitive OPTION(S);
// `g` = take the LAST marker / strip all. Group 1 = optional "S"; group 2 = labels.
//
// The optional `(?:\([^\s()]*\))?` after the `]` tolerates a stray markdown-link
// close that models sometimes append, e.g. `[OPTIONS: A | B](OPTIONS)`. Without it
// that suffix (a) breaks the end anchor so the marker leaks unparsed and (b) forms a
// valid `[label](url)` link, so the dashboard renders the whole thing as a purple
// link instead of buttons. The `(` must abut the `]` (no gap), so real trailing
// prose or a spaced `] (note)` still fails the anchor and is preserved. The group is
// OUTSIDE the label capture, so choices are unaffected — and because the regex is
// used with `replace`, the stray `(...)` is stripped from the displayed text too.
// The inner class is `[^\s()]` (not `[^)\n]`) so it shares no character with the
// trailing `[ \t]*` — that keeps the group unambiguous and ReDoS-safe (mirrors the
// backend OPTIONS_RE_LINE). The real tic contains no whitespace, so nothing is lost.
//
// The closing-bracket class (ASCII `]` plus the fullwidth / CJK lookalikes
// `】` U+3011, `］` U+FF3D, `〕` U+3015) mirrors the backend's `MARKER_CLOSERS`. The prompt only ever specifies ASCII `]`, but a
// model intermittently substitutes a lookalike, and a single wrong codepoint
// breaks the end anchor — the marker then leaks into the message as literal text
// and the turn silently loses its pills. Labels are unaffected, so accepting the
// lookalike costs nothing. ReDoS profile is unchanged from the previous literal
// `\]`: the class shares no character with the trailing `[ \t]*`, and the
// tempered body already admitted `]` via `[^[\n]`.
//
// Only use with String#matchAll and String#replace (which don't carry the global
// regex `lastIndex` hazard); do NOT call `.exec`/`.test` on this shared const.
export const OPTION_MARKER_RE =
  /\[OPTION(S)?:((?:[^[\n]|\[(?!OPTIONS?:))*)[\]\u3011\uFF3D\u3015](?:\([^\s()]*\))?[ \t]*$/gim

/** The closing brackets OPTION_MARKER_RE accepts — ASCII plus the CJK lookalikes.
 *  Module-private and used with matchAll only (to take the LAST closer in the
 *  probed body), so the g-flag `lastIndex` hazard never applies. */
const CLOSER_RE = /[\]\u3011\uFF3D\u3015]/g

/** What follows the LAST closer when the label list is still being written.
 *
 * A label may legitimately contain a closer (`[OPTIONS: Alpha ] | Bravo ]]` is a
 * supported, tested shape), so a closer alone does not mean the marker ended. The
 * label grammar is separator-joined, so a run of labels that CONTINUES resumes
 * with `|` (or `,`) after that closer. Anything else — ordinary words — means the
 * marker closed and prose followed it on the same line, which is the shape
 * OPTION_MARKER_RE deliberately declines to parse and which must therefore stay
 * visible. Without this discriminator the two failure modes trade places: keying
 * on "any closer arrived" releases a bracket-bearing label back into the prose,
 * and cutting unconditionally hides a genuine sentence like
 * `Explain the literal [OPTIONS:] syntax here` for the rest of the turn. */
const CONTINUES_LABELS_RE = /^[ \t]*[|,]/

/** A COMPLETE head in any casing. Two fixed literals under one optional `S`, so
 *  it cannot backtrack. Module-private and used with matchAll only (to take the
 *  LAST head in the probed tail), so the g-flag `lastIndex` hazard never applies. */
const HEAD_RE = /\[OPTIONS?:/gi

/** A head that is still being TYPED — every prefix of `[OPTIONS:` / `[OPTION:`,
 *  from the bare `[` up to the full head, spelled as nested optionals.
 *
 * A half-typed head is genuinely ambiguous: `[OPTION` can still become either
 * `[OPTIONS:` (a marker) or `[Optional]` (real prose). Casing is the only signal
 * available before the colon arrives, which is why these are two CASE-CONSISTENT
 * patterns rather than one `i`-flagged pattern: all-caps (the canonical form the
 * prompt specifies) or all-lower. That releases `[Optional]` after two characters
 * instead of holding it for eight. The cost is bounded and one-directional: a
 * mixed-case head like `[Options:` stays visible for the width of the head and is
 * then caught by HEAD_RE the moment its colon lands — whereas a false hold on
 * prose would swallow real content. HEAD_RE itself stays case-INSENSITIVE, because
 * a complete head is unambiguous in any casing. Both are anchored and non-global,
 * so `.test` on them is safe. */
const PARTIAL_HEAD_UPPER_RE = /^\[(?:O(?:P(?:T(?:I(?:O(?:N(?:S?:?)?)?)?)?)?)?)?$/
const PARTIAL_HEAD_LOWER_RE = /^\[(?:o(?:p(?:t(?:i(?:o(?:n(?:s?:?)?)?)?)?)?)?)?$/

/** How far back from the live edge stripPartialOptionMarker probes. A marker
 *  line is short, so this only ever clips pathological single-line output — and
 *  it keeps the per-frame cost constant however long the stream buffer grows. */
const TAIL_SCAN = 4096

/** Drop the whitespace a removed fragment sat behind, so the markdown renderer
 *  never sees a dangling blank line or trailing space where the marker was. */
function cutAt(text: string, idx: number): string {
  return text.slice(0, idx).trimEnd()
}

/**
 * Hide a marker that is only PARTIALLY streamed — the streaming counterpart to
 * OPTION_MARKER_RE.
 *
 * OPTION_MARKER_RE anchors on a closing bracket that ends the line, so it cannot
 * match a marker whose `]` has not arrived yet. During the reveal that leaves a
 * window (one to a few hundred deltas, i.e. the width of the marker line) where
 * the raw `[OPTIONS: Merge it now | Show me the d…` types itself out as prose
 * and then vanishes into pills at turn end. This suppresses the growing tail so
 * the marker is never visible in either form.
 *
 * An unterminated marker is by construction at the tail of the buffer, so only
 * the last line — clipped to TAIL_SCAN — is examined. The window is sliced FIRST
 * and the line break located inside it, so the probe cost is bounded by
 * TAIL_SCAN rather than by the buffer: a newline-free multi-megabyte stream would
 * otherwise make the `\n` search alone scan the whole buffer on every frame. Plain
 * indexOf on the bounded slice, not a regex scan of the content — linear, no
 * backtracking, no ReDoS surface added to the synchronous render path.
 *
 * Two shapes are recognized:
 *   1. a complete head whose marker is still being WRITTEN → cut at the head.
 *      "Still being written" is not "no closing bracket yet": a label may
 *      legitimately contain one, so the test is whether the label list continues
 *      after the last closer (see CONTINUES_LABELS_RE). A head whose marker
 *      closed and was followed by ordinary same-line prose is left alone — that
 *      prose is real content, and OPTION_MARKER_RE deliberately declines to
 *      parse that shape as a marker.
 *   2. mid-head, i.e. the tail is still a prefix of a head (`[`, `[OPT`,
 *      `[OPTIONS`) → cut at the `[`. Because a half-typed head is ambiguous with
 *      ordinary prose, this branch is doubly constrained: the `[` must open a
 *      line or follow whitespace (so `arr[0` is never touched), and the prefix
 *      casing must be consistent (see PARTIAL_HEAD_UPPER_RE).
 *
 * Cutting is safe in case 1 because `parseOptions` runs FIRST, so a head reaching
 * this function almost always belongs to a marker that is not yet
 * complete-and-line-final. "Almost": a SAME-LINE PAIR survives it, because
 * OPTION_MARKER_RE's tempered body cannot match the earlier of two markers on one
 * line, so `[OPTIONS: A] [OPTIONS: B]` loses only the second and the first arrives
 * here complete. Hiding it mid-stream is the wanted behaviour anyway — it is a
 * marker, not prose — and the isStreaming gate returns it at turn end.
 *
 * The residual limit, stated so it is not mistaken for an oversight: a label that
 * contains a closer AND continues with words rather than a separator
 * (`[OPTIONS: Fix ] logging | Skip`) is visible between that closer and the next
 * separator. Both alternatives are worse — keying on "a closer arrived" releases
 * the whole marker, and cutting unconditionally swallows a genuine sentence.
 *
 * Call this ONLY while a message is streaming. On a finished message an
 * unterminated marker is real content — prose that happens to discuss the
 * syntax, or a truncated turn — and must render as written.
 */
export function stripPartialOptionMarker(text: string): string {
  const from = Math.max(0, text.length - TAIL_SCAN)
  const window = text.slice(from)
  if (!window.includes('[')) return text
  const nl = window.lastIndexOf('\n')
  const start = from + nl + 1
  const tail = window.slice(nl + 1)
  if (!tail.includes('[')) return text

  let head = -1
  for (const m of tail.matchAll(HEAD_RE)) head = m.index
  if (head >= 0) {
    const body = tail.slice(head)
    let closer = -1
    for (const m of body.matchAll(CLOSER_RE)) closer = m.index
    // No closer yet, or the label list resumes after it → still being written.
    // A closer with only trailing blanks after it cannot actually reach here
    // (parseOptions would have stripped that marker), so it falls in with "cut".
    const rest = closer < 0 ? '' : body.slice(closer + 1)
    const forming = closer < 0 || rest.trim() === '' || CONTINUES_LABELS_RE.test(rest)
    return forming ? cutAt(text, start + head) : text
  }

  const open = tail.lastIndexOf('[')
  const abs = start + open
  // The canonical marker opens its own line; the same-line variant the regex
  // also accepts still has a space before the `[`. Requiring that boundary
  // costs the marker nothing and takes every in-word bracket (`arr[0`, a
  // footnote ref) out of scope entirely.
  if (abs > 0 && !/\s/.test(text[abs - 1])) return text
  const frag = tail.slice(open)
  const partial = PARTIAL_HEAD_UPPER_RE.test(frag) || PARTIAL_HEAD_LOWER_RE.test(frag)
  return partial ? cutAt(text, abs) : text
}
