// Canonical marker regexes — the single source of truth for the frontend, mirroring the
// backend's ReDoS-hardened OPTIONS_RE_LINE / OPTION_ACTIONS_RE_LINE
// (src/kiro_crew/constants.py). Import these instead of hand-rolling a copy so the
// grammar can't drift between the dashboard's several parsers.
//
// TWO markers live here, deliberately siblings rather than one pattern with a mode flag:
// `[OPTION(S): a | b]` offers CONTENT choices that are sent as the next message, and
// `[OPTION-ACTIONS: close=label]` offers a LOCAL UI action that runs with no LLM turn.
// See OPTION_ACTION_MARKER_RE below for why the head is distinct rather than encoded in a
// label, and for the non-collision property that makes the split safe.
//
// The tempered body `(?:[^[\n]|\[(?!OPTIONS?:|OPTION-ACTIONS:))*` matches any run of
// characters that does NOT begin a fresh marker of EITHER kind, which gives three
// properties:
//   1. a label may itself contain `]` — the block ends at the LAST `]` that ends the
//      line, not the first `]` (so "[OPTIONS: a] | b]]" → ["a]", "b]"]);
//   2. two same-line markers can't merge into one garbage label — and because the
//      temper names BOTH heads, that holds for a MIXED same-line pair too
//      (`[OPTIONS: A] [OPTION-ACTIONS: close=B]`), not just two of a kind;
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
// `String#replace` is the only use that is safe on this shared const as-is: it resets `lastIndex`.
// `String#matchAll` does NOT — it seeds its internal clone from `lastIndex`, so pass a fresh
// `new RegExp(OPTION_MARKER_RE)` there. Never call `.exec`/`.test` on it: both leave the index
// advanced, and the next reader silently scans from the wrong offset. Both hazards apply
// verbatim to OPTION_ACTION_MARKER_RE below — they are properties of the `g` flag, not of
// which head the pattern carries.

/** Every protocol head a tempered body must refuse to cross, as ONE alternation shared
 *  by both patterns below. Longest-distinguishing first, mirroring the backend's
 *  `MARKER_PREFIXES` order. `OPTIONS?:` covers `[OPTIONS:` and the single-choice
 *  `[OPTION:`; `OPTION-ACTIONS:` is a genuinely separate literal rather than a case of
 *  it — the two strings diverge at `S` vs `-`, so `"[OPTION-ACTIONS:"` does not start
 *  with `"[OPTIONS:"` and a single-head temper does not cover it.
 *
 * The tempering exists for ReDoS (see the block above), but once a SECOND head exists it
 * also carries a correctness property the single-head version never needed: a body that
 * forbids only its OWN head still happily consumes the OTHER one. MEASURED on the
 * backend's `OPTIONS_RE_TRAILER` before its heads were shared — given
 * `"[OPTIONS: a | b]\n[OPTION-ACTIONS: close=X]"` its body crossed the second marker and
 * captured `" a | b]\n[OPTION-ACTIONS: close=X"`, so the action marker's raw text became
 * a BUTTON LABEL and the real second choice was lost. Silent: the regex matches, the
 * anchor is satisfied, and only the capture is wrong.
 *
 * Both frontend patterns are LINE forms whose bodies exclude `\n`, so only a SAME-LINE
 * pair (`[OPTIONS: A] [OPTION-ACTIONS: close=B]`) can reach it here — but that shape IS
 * reachable, and "which shapes are currently reachable" is a property of today's call
 * sites rather than of the grammar. Both heads are therefore excluded from both bodies.
 *
 * Adding a head stays linear: at each position either the character is not `[` (first
 * alternative) or it is and the lookahead alone decides — the alternatives remain
 * mutually exclusive, so no new backtracking path appears. */
// Every fragment below is a REGEX LITERAL read through `.source`, never a quoted
// string. Two reasons, and the second is why it is worth the `.source` noise: the
// engine parses each fragment at author time, so a malformed class or an unbalanced
// group is a syntax error here rather than a runtime throw from `new RegExp`; and a
// literal needs one level of backslash instead of two, which is what keeps
// `[^[\n]` and `\u3011` readable. The i18n string-literal gate also reads a quoted
// grammar fragment as user-facing copy, which it is not.
const MARKER_HEADS = /OPTION-ACTIONS:|OPTIONS?:/.source
/** A `[` that does NOT open a fresh marker of either kind. */
const TEMPER = `\\[(?!${MARKER_HEADS})`
/** One ordinary body character: anything but a `[` that could start a head, and not a
 *  newline (a negated class matches `\n` regardless of flags, so it must be explicit). */
const BODY_CHAR = /[^[\n]/.source
/** The captured label body, single-line. Exactly one capture group. */
const BODY_LINE = `((?:${BODY_CHAR}|${TEMPER})*)`
/** The closer class, the optional stray markdown-link close, and the trailing blanks. */
const TAIL_CLOSER = /[\]\u3011\uFF3D\u3015](?:\([^\s()]*\))?[ \t]*/.source
/**
 * Where a marker line may END: the `m`-flag end anchor, OR immediately before a
 * SIBLING MARKER on the same line.
 *
 * Requiring `$` alone meant that on a shared line only the TRAILING marker could
 * match. `[OPTIONS: A] [OPTION-ACTIONS: close=B]` matched the action marker and left
 * the content marker unmatched, so its pills were dropped AND its raw text rendered
 * as prose — and the affordance that survived was the one that deletes the tab. The
 * same held for a same-kind pair, where the earlier marker leaked.
 *
 * The alternative is a LOOKAHEAD, so the sibling is not consumed and remains
 * available to its own pattern; both markers therefore parse from one line. It costs
 * no backtracking: at the terminator position either the anchor holds or the
 * lookahead decides in O(1), and the body is still tempered against every head so it
 * cannot cross into the sibling to begin with.
 *
 * Deliberately NOT "anything may follow": a marker trailed by ordinary words stays
 * unparsed, which is what keeps a sentence discussing the syntax rendering as
 * written. Only a sibling marker terminates early.
 */
const TAIL_LINE = `${TAIL_CLOSER}(?:$|(?=\\[(?:${MARKER_HEADS})))`

// Composed from the shared pieces rather than spelled twice: the two markers fail the
// same way (a CJK closer, a stray `(OPTIONS)` tic, a same-line sibling), so a grammar
// improvement to one that missed the other would be a silent regression. Group 1 = the
// optional "S"; group 2 = the labels.
export const OPTION_MARKER_RE = new RegExp(`\\[OPTION(S)?:${BODY_LINE}${TAIL_LINE}`, 'gim')

/**
 * The zero-turn UI-action marker — `[OPTION-ACTIONS: close=Nothing else, close this session]`.
 *
 * A SIBLING of OPTION_MARKER_RE, not an extension of it, and the distinct head is the
 * entire mechanism. The body is `|`-separated `<action>=<label>` entries where the action
 * is a STRICT ENUM (this ships exactly `close`) and the label — everything after the
 * FIRST `=` — is arbitrary free text. `parseOptions` does that splitting; this pattern
 * only isolates and strips the block. Group 1 = the raw entry list.
 *
 * WHY a separate head instead of a reserved label inside `[OPTIONS:]`: option labels are
 * model-emitted prose, so any in-band encoding means an agent that merely WRITES ABOUT
 * this feature emits a live close button and tears down the user's tab. The action
 * therefore occupies its own field and the label is never load-bearing.
 *
 * NON-COLLISION, in both directions, is the property the whole design rests on, and it is
 * structural rather than incidental: OPTION_MARKER_RE requires `OPTIONS:` or `OPTION:`
 * immediately after the `[`, which `[OPTION-` cannot supply; this pattern requires the
 * literal `OPTION-ACTIONS:`, which a bare `[OPTIONS:` cannot supply. Neither can ever
 * parse the other's marker as its own, so an action marker never yields content choices
 * and a content marker never yields an action. Pinned in BOTH directions by
 * `src/test/optionActions.test.ts` — a test rather than a comment, because it is the
 * assumption every other part of the design leans on.
 *
 * Grammar is otherwise IDENTICAL to OPTION_MARKER_RE, by construction (same shared body
 * and tail above), because the failure modes are the same failure modes and here a broken
 * end anchor is strictly worse than a lost button: the marker leaks as literal text.
 *
 * Same `g`-flag hazards as OPTION_MARKER_RE: `replace` only; clone for `matchAll`; never
 * `.exec`/`.test`.
 */
export const OPTION_ACTION_MARKER_RE = new RegExp(
  `\\[OPTION-ACTIONS:${BODY_LINE}${TAIL_LINE}`,
  'gim',
)

/**
 * Remove EVERY marker of BOTH kinds. The one strip a consumer outside this module
 * should ever call.
 *
 * Exists because "keyed on one head" is a repeat defect, not a hypothetical:
 * `"[OPTION-ACTIONS:"` does not start with `"[OPTIONS:"` — they diverge at `S` vs
 * `-` — so a site that strips only `OPTION_MARKER_RE` passes the action marker
 * through untouched while looking correct. That shape was counted at three separate
 * consumers (a hand-back probe, a substance measure, and the search index), each
 * failing differently: a marker-only row buried in the collapse pane, marker text
 * counted as prose, and a search hit inside text the user can never see highlighted.
 *
 * A FUNCTION rather than an exported regex, deliberately, for the reason
 * `protocol/index.ts` gives for withholding the patterns themselves: they are
 * g-flagged, so handing one across a module boundary hands out mutable `lastIndex`
 * state. `.replace()` with a `g` regex resets that index itself, so this is safe to
 * call repeatedly and from anywhere, and no caller has to know the hazard exists.
 *
 * Content marker first, matching `parseOptions`. Order is not cosmetic: each pattern
 * anchors on ending its own line, so removing one can let the other reach an anchor
 * it could not before.
 */
export function stripOptionMarkers(text: string): string {
  return stripActionMarkers(text.replace(OPTION_MARKER_RE, ''))
}

/** The closing brackets OPTION_MARKER_RE accepts — ASCII plus the CJK lookalikes.
 *  Module-private and used with matchAll only (to take the LAST closer in the
 *  probed body), so the g-flag `lastIndex` hazard never applies. */
const CLOSER_RE = /[\]\u3011\uFF3D\u3015]/g

/** The openers those closers pair with, in the same order — mirrors `MARKER_OPENERS`.
 *  A closer has to know which bracket it closes, or a citation `[1]` cancels an open head. */
const OPENER_RE = /[[\u3010\uFF3B\u3014]/g

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

/** A COMPLETE head of EITHER kind, in any casing. Built from the same `MARKER_HEADS`
 *  alternation the tempered body excludes, so a head that one recognises is never a head
 *  the other misses — the single-literal version silently skipped `[OPTION-ACTIONS:`,
 *  because `"[OPTION-ACTIONS:"` does not start with `"[OPTIONS:"`. Two fixed literals
 *  under one optional `S`, so it cannot backtrack. Module-private and used with matchAll
 *  only (to take the LAST head in the probed tail), so the g-flag `lastIndex` hazard
 *  never applies. */
const HEAD_RE = new RegExp(`\\[(?:${MARKER_HEADS})`, 'gi')

/**
 * For each ASCENDING offset in `starts`, does it sit inside an EARLIER UNCLOSED marker?
 *
 * Linear. The shape this replaces re-scanned the whole line prefix — and rebuilt a
 * `RegExp` — once per match, so one long line carrying `k` markers cost O(n*k). A 104k
 * character single-line model response with 4000 action markers stalled for over a
 * second on the backend twin; the same shape ran here on the render path. Indexing each
 * class once and walking three monotonic pointers is O(n + k).
 *
 * Callers must pass offsets left to right, which `matchAll` and `replace` both do. The
 * pointers only advance, so an out-of-order offset reads a stale window rather than
 * throwing — hence stating the requirement here.
 *
 * The content pattern's body is tempered against every head, so it cannot cross into a
 * nested action marker — and with no closer before that head the content marker fails to
 * match at all. The action pattern scans INDEPENDENTLY, so it matched the nested span
 * regardless and the row rendered a live `close` chip out of text the reader sees as
 * broken syntax. One dropped `]` in model-emitted prose is enough to reach it.
 *
 * `close` tears the tab down, so an unparseable line must offer NOTHING rather than
 * degrade to the single affordance that deletes state. The refusal lives HERE, at the
 * matcher, rather than in a consumer: a rejected span is not a marker, so it must also
 * not be STRIPPED — it stays visible as written, exactly as `[OPTIONS: A] for details`
 * does. A downstream sanitiser could suppress the chip but would still have excised the
 * text, hiding half the malformed line.
 *
 * A per-line bracket DEPTH decides it, not the last head against the last closer. That
 * pairwise form read a BALANCED nested pair as closing the OUTER head: given
 * `[OPTIONS: x [OPTION-ACTIONS: a] [OPTION-ACTIONS: b]` the first pair supplied both the
 * last head and the last closer, so `b` was accepted and rendered a chip with the outer
 * head still open.
 *
 * Depth counts HEAD brackets only, and a closer pops whichever bracket is innermost. A
 * bare count was wrong the same way one step down: a citation `[1]` inside an open head
 * supplied a closer that cancelled the head, so
 * `[OPTIONS: see [1] for details [OPTION-ACTIONS: close=X]` rendered a live close chip
 * from syntax matching no content marker, while the same line without the citation
 * suppressed it. A stray closer pops an empty stack, which is a no-op, and the stack is
 * reset at each newline because both heads are LINE forms — a head on a PRIOR line cannot
 * poison this one.
 */
function unclosedMarkerFlags(text: string, starts: number[]): boolean[] {
  const positions = (re: RegExp): number[] => {
    const out: number[] = []
    // Clone per scan: the source patterns are `g`-flagged, so a shared traversal would
    // seed from a stale `lastIndex`. Same rule the rest of this module follows.
    for (const m of text.matchAll(new RegExp(re))) if (m.index !== undefined) out.push(m.index)
    return out
  }
  const heads = positions(HEAD_RE)
  const openers = positions(new RegExp(OPENER_RE.source, 'g'))
  const closers = positions(new RegExp(CLOSER_RE.source, 'g'))
  const newlines = positions(/\n/g)
  let headI = 0
  let openI = 0
  let closeI = 0
  let lineI = 0
  // Innermost-last: `true` marks a marker head, `false` any other bracket. `depth` counts
  // the head frames, so the flag stays O(1) per offset -- see this function's doc comment.
  const stack: boolean[] = []
  let depth = 0
  return starts.map(start => {
    // OFFSET order matters: a closer must pop the bracket it actually closes, so draining
    // openers before closers mispairs them. Each pointer only moves forward.
    for (;;) {
      const o = openI < openers.length && openers[openI] < start ? openers[openI] : Infinity
      const c = closeI < closers.length && closers[closeI] < start ? closers[closeI] : Infinity
      const n = lineI < newlines.length && newlines[lineI] < start ? newlines[lineI] : Infinity
      const next = Math.min(o, c, n)
      if (next === Infinity) break
      if (next === n) {
        // Both heads are LINE forms, so an unclosed head cannot reach past its newline.
        stack.length = 0
        depth = 0
        lineI++
      } else if (next === o) {
        // A head IS an opener, so the ascending head pointer classifies it in O(1).
        while (headI < heads.length && heads[headI] < next) headI++
        const isHead = headI < heads.length && heads[headI] === next
        if (isHead) {
          headI++
          depth++
        }
        stack.push(isHead)
        openI++
      } else {
        // Pops the INNERMOST bracket: a citation's closer must not cancel a real head, and
        // a stray closer with no opener pops an empty stack, which is a no-op.
        if (stack.pop() === true) depth--
        closeI++
      }
    }
    return depth > 0
  })
}

/** Every action marker in `text` that is genuinely a marker — nested-in-an-unclosed-head
 *  matches are dropped. The one scan a consumer should use; scanning
 *  `OPTION_ACTION_MARKER_RE` directly re-introduces the live-chip-from-broken-syntax
 *  defect this filter exists to close. */
export function matchActionMarkers(text: string): RegExpMatchArray[] {
  const matches = [...text.matchAll(new RegExp(OPTION_ACTION_MARKER_RE))]
  const flags = unclosedMarkerFlags(
    text,
    matches.map(m => m.index ?? 0),
  )
  return matches.filter((m, i) => m.index === undefined || !flags[i])
}

/** Remove every action marker that is genuinely a marker, leaving a rejected nested span
 *  visible. Paired with `matchActionMarkers` so what is OFFERED and what is HIDDEN can
 *  never disagree — the pair disagreeing is how a chip appears for text still on screen,
 *  or text vanishes with no chip to show for it. */
export function stripActionMarkers(text: string): string {
  const starts = [...text.matchAll(new RegExp(OPTION_ACTION_MARKER_RE))].map(m => m.index ?? 0)
  const flags = unclosedMarkerFlags(text, starts)
  const inside = new Map(starts.map((s, i) => [s, flags[i]]))
  return text.replace(OPTION_ACTION_MARKER_RE, (m: string, _body: string, offset: number) =>
    inside.get(offset) ? m : '',
  )
}

/** Every prefix of `head`, spelled as nested optionals — `prefixChain('AB:')` yields
 *  `(?:A(?:B(?::)?)?)?`, which matches ``, `A`, `AB` and `AB:` and nothing else.
 *
 * Derived from the literal rather than hand-nested: the merged tree for three heads
 * sharing the prefix `OPTION` is where a miscounted parenthesis would sit, and a
 * miscount here does not fail loudly — it just holds or releases the wrong fragment
 * mid-stream. Adding a head is one array entry. The generated shape carries no
 * repetition quantifier at all, so it stays backtrack-free. */
const prefixChain = (head: string): string =>
  [...head].reduceRight(
    (inner, ch) => `(?:${ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}${inner})?`,
    '',
  )

/** The heads a partially-typed marker can still become, WITHOUT the leading `[` (the
 *  anchored patterns below spell that once). `OPTION:` is listed separately from
 *  `OPTIONS:` because a prefix chain has no optional letter in the middle. */
const TYPEABLE_HEADS = [/OPTIONS:/.source, /OPTION:/.source, /OPTION-ACTIONS:/.source]

/** A head that is still being TYPED — every prefix of `[OPTIONS:` / `[OPTION:` /
 *  `[OPTION-ACTIONS:`, from the bare `[` up to the full head.
 *
 * A half-typed head is genuinely ambiguous: `[OPTION` can still become `[OPTIONS:`,
 * `[OPTION-ACTIONS:` (both markers) or `[Optional]` (real prose). Casing is the only
 * signal available before the colon arrives, which is why these are two CASE-CONSISTENT
 * patterns rather than one `i`-flagged pattern: all-caps (the canonical form the prompt
 * specifies) or all-lower. That releases `[Optional]` after two characters instead of
 * holding it for eight — and holding it for SIXTEEN once the longer action head is in
 * scope, which is why the rule matters more now, not less. The cost is bounded and
 * one-directional: a mixed-case head like `[Options:` stays visible for the width of the
 * head and is then caught by HEAD_RE the moment its colon lands — whereas a false hold on
 * prose would swallow real content. HEAD_RE itself stays case-INSENSITIVE, because a
 * complete head is unambiguous in any casing. Both are anchored and non-global, so
 * `.test` on them is safe. */
const PARTIAL_HEAD_UPPER_RE = new RegExp(
  `^\\[(?:${TYPEABLE_HEADS.map(h => prefixChain(h)).join('|')})$`,
)
const PARTIAL_HEAD_LOWER_RE = new RegExp(
  `^\\[(?:${TYPEABLE_HEADS.map(h => prefixChain(h.toLowerCase())).join('|')})$`,
)

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
 * OPTION_MARKER_RE and OPTION_ACTION_MARKER_RE alike.
 *
 * Both patterns anchor on a closing bracket that ends the line, so neither can
 * match a marker whose `]` has not arrived yet. During the reveal that leaves a
 * window (one to a few hundred deltas, i.e. the width of the marker line) where
 * the raw `[OPTIONS: Merge it now | Show me the d…` — or
 * `[OPTION-ACTIONS: close=Nothi…` — types itself out as prose and then vanishes
 * into pills or a chip at turn end. This suppresses the growing tail so the
 * marker is never visible in either form. The action marker needs this at least
 * as much: it renders as a single chip, so the raw text is a larger fraction of
 * what the user briefly sees.
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
 *      `[OPTIONS`, `[OPTION-ACT`) → cut at the `[`. Because a half-typed head is
 *      ambiguous with ordinary prose, this branch is doubly constrained: the `[`
 *      must open a line or follow whitespace (so `arr[0` is never touched), and
 *      the prefix casing must be consistent (see PARTIAL_HEAD_UPPER_RE).
 *
 * Cutting is safe in case 1 because `parseOptions` runs FIRST — and it strips
 * markers of BOTH kinds — so a head reaching this function belongs to a marker that
 * is not yet complete-and-line-final.
 *
 * A SAME-LINE PAIR used to be the exception: the tail required `$`, so the earlier of
 * two markers on one line could not match, `[OPTIONS: A] [OPTIONS: B]` (or a mixed
 * pair) kept only the last, and the first arrived here complete. The tail now also
 * terminates before a sibling marker, so `parseOptions` consumes BOTH and nothing
 * complete reaches this function from that shape — which is why this paragraph
 * records history rather than a live caveat.
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
