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
// The tempered body matches any run of characters in which no alternative begins a fresh
// marker of EITHER kind — both bracket forms carry that guard — which gives three
// properties:
//   1. a label may itself contain a closer, so the block does not end at the FIRST
//      one (so "[OPTIONS: a] | b]]" → ["a]", "b]"]) — but it is admitted
//      CONDITIONALLY, not freely: see #9284 below for the rule and why an
//      unconditional `]` ran the body past the marker and deleted prose;
//   2. two same-line markers can't merge into one garbage label — including a
//      NESTED head, which the pair form's own head guard is what preserves, and
//      because the temper names BOTH heads that holds for a MIXED same-line pair too
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
// `\]`: the class shares no character with the trailing `[ \t]*`, and the body
// excludes it from its negated class and readmits all four codepoints in exactly
// TWO tempered places (below) — as the matched-pair form's final atom, and via the
// continuation lookahead. A widening of this class has to be re-audited against
// both; the pair form is the one holding the deciding lookahead, so it is the one
// not to skip.
//
// A CLOSER MUST BE MATCHED, OR CONTINUE THE LABEL LIST (#9284). A label may
// legitimately carry a closer (`[OPTIONS: Alpha ] | Bravo ]]` is a supported,
// tested shape), so the body has to admit one — but admitting it UNCONDITIONALLY
// (the old `[^[\n]`, which includes `]`) made the body run to the LAST closer in
// range instead of the first plausible one. An ordinary final line that mentions
// a bracket after the marker then matched across BOTH — `Use [OPTIONS: A | B]
// then check arr[0]` matched whole — and since the marker is removed by
// `replace`, the sentence vanished from the message and came back as a pill
// label. So the body now admits a closer under either of two conditions, because
// neither alone separates the three shapes that matter:
//
//   [OPTIONS: Fix [x] logging | Skip]        MATCHED by an earlier `[` -> parses
//   [OPTIONS: Alpha ] | Bravo ]]            list CONTINUES after it   -> parses
//   Use [OPTIONS: A | B] then check arr[0]  neither                   -> declined
//
// The second is exactly the rule `CONTINUES_LABELS_RE` below already applies to
// the STREAMING probe, now applied by the grammar too, so the two stop answering
// the same question differently. The first is required by `Fix [x] logging`,
// which the backend's `test_parse_options.py` pins as a supported shape outright.
// The two are kept DISJOINT by what follows the closer — the matched form
// requires that its closer NOT be followed by a separator or another closer,
// which is precisely when the continuation form applies — so no span has two
// parses and the body stays linear despite two bracket alternatives.
//
// RESIDUAL COST: every shape given up is a closer that satisfies NEITHER half and
// has ordinary words after it, where the input is genuinely indistinguishable from
// "marker ended, prose followed". There is more than one way to be that closer,
// and all of them parsed on the old body:
//
//   [OPTIONS: Fix ]x logging | Skip]             unmatched — no `[` at all
//   [OPTIONS: Fix list[dict[str, Any]] now | S]  nesting deeper than one level
//   [OPTIONS: 【重要】修复 | 跳过】                 a lookalike PAIR — `【` is not an
//                                                opener, only `[` is
//
// All fail toward a VISIBLE marker, not toward deleted prose, and that asymmetry
// is what makes them affordable. Making them parse means matching brackets to
// arbitrary depth and over an opener set this grammar does not have. The
// separator-tail form (`…| Wait], details in CHANGELOG[1]`) is NOT reachable by
// this rule: `], ` does continue the list, by the same rule that makes
// `[OPTIONS: Alpha ], Bravo]` legal. What decides it is the TERMINATOR GATE below,
// reaching it from the other end — the `[` of `CHANGELOG[1]` is the opener whose
// partner would end the marker, so the line is declined and left whole.
//
// A BARE OPENER MAY NOT BE THE ONE WHOSE PARTNER CLOSER ENDS THE MARKER. The bare
// form exists so a stray opener does not sink a whole marker, but it also admitted
// the opener in a marker the model never closed — `[OPTIONS: A | B then check
// arr[0]`, where the only closer belongs to `arr[0]`. The body ran on through the
// prose, that `]` became the terminator, and since the marker is removed by
// `replace` the line left the message and came back as the pill label
// `B then check arr[0`.
//
// No rule over bracket structure can separate the two: reduced to skeletons,
// `[OPTIONS: Fix | Skip [x logging]` and `[OPTIONS: A | B then check arr[0]` are
// the same string. The discriminator is where the opener sits relative to the END,
// so the bare form is refused when nothing but ordinary text lies between it and a
// closer at the end anchor. Crossing `|` clears it — the opener is inside a label
// and the list continues past it, which is what keeps the pinned stray-opener
// shape — and so does another bracket, because some other form owns that closer.
// `,` does NOT clear it: a comma is only the fallback separator, and inside brackets
// it is ordinary punctuation (`dict[str, Any]`), so a scan that stopped there would
// halt before the closer and let the shape through.
//
// MARKDOWN WRAPPERS (#9110): a model sometimes wraps the whole marker line in
// inline code or emphasis — `` `[OPTIONS: A | B]` `` or `**[OPTIONS: A | B]**`.
// The wrapper character lands AFTER the closer, breaks the end anchor, and the
// marker leaks as literal (code-styled) text while the turn loses its pills —
// the same class of tic as the stray `](OPTIONS)` suffix above. The grammar
// absorbs a wrapper run of `` ` `` / `*` / `_` up to 3 long (`***` is the
// longest CommonMark emphasis run; 4+ is not a wrapper): LEADING only at line
// start after optional indent (so emphasis belonging to preceding prose, like
// `**Choose:** [OPTIONS: …]`, is never eaten — the marker still parses, the
// emphasis stays), TRAILING only when the marker itself OPENED one: the first
// alternation branch requires a NONEMPTY leading run (`{1,3}`) before it may
// absorb a trailing run; the second branch is the pre-widening grammar, byte
// for byte, for bare and mid-line markers. That balanced requirement is the
// invariant that keeps every wrapper-shaped piece of enclosing Markdown
// intact at once: a mid-line code span's closer (`` `Use [OPTIONS: A | B]` ``),
// and a MULTILINE emphasis closer (`**Choose one\n[OPTIONS: A | B]**`) — under
// CommonMark flanking rules a run at line start can only OPEN emphasis, so a
// leading run is safely the marker's own, while a trailing run without one
// belongs to the prose and must survive the strip. Leading-only still parses
// (nothing follows the closer, so nothing can be stolen); trailing-only
// renders literally, as it did before the widening. JS has no conditional
// groups, hence the alternation: groups 1/2 are the anchored-wrapped branch's
// (S, labels), groups 3/4 the bare branch's — exactly one pair is defined per
// match, so read `m[1] ?? m[3]` / `m[2] ?? m[4]` (parseOptions does). ReDoS
// profile unchanged: each branch is the proven linear shape, a position is
// attempted against at most both, and the wrapper class shares no character
// with the indent/trailing `[ \t]*`.
// Mirrors the backend's MARKER_WRAPPERS + `(?(lwrap)...)` conditional
// (constants.py).
// MODULE-PRIVATE, and deliberately so: this pattern finds CANDIDATE markers, and a
// candidate is not yet a marker. Whether its terminating closer is really its own
// cannot be decided here — see `labelsHaveUnmatchedOpener` below — so handing the pattern
// out would hand out a way to skip that decision. `findOptionMarkers`,
// `findLastOptionMarker` and `stripOptionMarkers` are the API; they apply both halves
// and clone the regex internally, which also retires the `lastIndex` hazard that used
// to be every caller's problem.
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
/** One ordinary body character: anything but a `[` that could start a head, not a
 *  closer (those are admitted only conditionally, below), and not a newline (a negated
 *  class matches `\n` regardless of flags, so it must be explicit). */
const CLOSER_CHARS = /\]\u3011\uFF3D\u3015/.source
const CLOSER_CLASS = `[${CLOSER_CHARS}]`
const BODY_CHAR = `[^[${CLOSER_CHARS}\\n]`
/** What must FOLLOW a closer for the label list to be continuing rather than ended:
 *  a separator, or another closer. */
const CONTINUES = `[ \\t]*[|,]|${CLOSER_CLASS}`
/**
 * A closer inside a label is admitted under EITHER of two conditions, never freely
 * (#9284). Admitting one unconditionally ran the body to the LAST closer on the line,
 * so an ordinary sentence mentioning a bracket after the marker was swallowed whole and
 * came back as a pill label; every consumer removes the whole match, so the sentence
 * vanished from the message.
 *
 *   MATCHED PAIR      `[OPTIONS: Fix [x] logging | Skip]`   the `[` is matched here
 *   LIST CONTINUES    `[OPTIONS: Alpha ] | Bravo ]]`        a separator follows
 *   NEITHER           `Use [OPTIONS: A | B] then arr[0]`    declined, stays prose
 *
 * The two are made disjoint by what follows the closer — the pair form requires that its
 * closer NOT be followed by a separator or another closer, which is exactly when the
 * continuation form applies — so no span has two parses and the body stays linear.
 */
const LABEL_PAIR = `${TEMPER}${BODY_CHAR}*${CLOSER_CLASS}(?!${CONTINUES})`
const LABEL_CONTINUES = `${CLOSER_CLASS}(?=${CONTINUES})`
/** The captured label body, single-line. Exactly one capture group. */
const BODY_LINE = `((?:${LABEL_PAIR}|${TEMPER}|${LABEL_CONTINUES}|${BODY_CHAR})*)`
/** The closer class, the optional stray markdown-link close, and the trailing blanks. */
const TAIL_CLOSE_RUN = /[\]\u3011\uFF3D\u3015](?:\([^\s()]*\))?/.source
const TAIL_SPACE = /[ \t]*/.source
const TAIL_CLOSER = `${TAIL_CLOSE_RUN}${TAIL_SPACE}`
/** One Markdown wrapper character — mirrors the backend's `MARKER_WRAPPERS`. */
const WRAP_CLASS = /[`*_]/.source
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
/** The wrapper-tolerant tail: the closer, then the run that only a marker which
 *  OPENED one may carry, then the same line end. */
const TAIL_LINE_WRAPPED = `${TAIL_CLOSE_RUN}${WRAP_CLASS}{0,3}${TAIL_SPACE}(?:$|(?=\\[(?:${MARKER_HEADS})))`

// Composed from the shared pieces rather than spelled twice: the two markers fail the
// same way (a CJK closer, a stray `(OPTIONS)` tic, a same-line sibling), so a grammar
// improvement to one that missed the other would be a silent regression. Group 1 = the
// optional "S"; group 2 = the labels.
const OPTION_MARKER_RE = new RegExp(
  `(?:^[ \\t]*${WRAP_CLASS}{1,3}\\[OPTION(S)?:${BODY_LINE}${TAIL_LINE_WRAPPED}`
    + `|\\[OPTION(S)?:${BODY_LINE}${TAIL_LINE})`,
  'gim',
)

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
const OPTION_ACTION_MARKER_RE = new RegExp(
  `\\[OPTION-ACTIONS:${BODY_LINE}${TAIL_LINE}`,
  'gim',
)


/** The pattern's source text, for the tests that pin its shape.
 *
 *  A string, not a regex, on purpose: a shape pin needs the characters, and handing
 *  back something callable would reopen the bypass the pattern's privacy closes. */
export const OPTION_MARKER_PATTERN_SOURCE = OPTION_MARKER_RE.source

/** Whether `labels` leave an opener unclosed, so the terminator is not theirs.
 *
 * The pattern finds candidates; this decides which candidates are markers, and it is
 * the whole reason the pattern is private. An unmatched opener means the closer the
 * pattern took as the terminator is really that opener's partner — so the marker was
 * never closed.
 *
 * What it prevents: `[OPTIONS: A | B then check arr[0]`, where the only closer
 * belongs to `arr[0]`. The body ran on through the prose, that `]` became the
 * terminator, and since the marker is removed by `replace` the line left the message
 * and came back as the pill label `B then check arr[0`.
 *
 * The pattern cannot do it: balance is not a regular language at unbounded depth, so
 * a lookahead sees one nesting level and `list[dict[str, int]]` defeats a one-level
 * rule, `a[b[c[d]]]` a two-level one.
 *
 * The rule is TOTAL — no separator escape hatch. An earlier form accepted an
 * unmatched opener when a `|` followed it; that hatch was defeated three times, most
 * recently by a `|` INSIDE the unmatched bracket (`dict[str | int]`), and each time
 * the shape it readmitted was structurally identical to the one it was meant to
 * protect. THE COST is exactly one shape: `[OPTIONS: Fix [x logging | Skip]`, a label
 * carrying an unclosed `[`, no longer parses — it renders as visible text, which is
 * the direction every cost in this grammar fails in.
 *
 * An unmatched CLOSER is ignored: a label may legitimately carry one
 * (`[OPTIONS: Alpha ] | Bravo ]]` is supported and tested).
 *
 * Mirrors `_marker_labels_have_unmatched_opener` in `constants.py`. */
export function labelsHaveUnmatchedOpener(labels: string): boolean {
  let depth = 0
  for (const ch of labels) {
    if (ch === '[') depth++
    else if (']】］〕'.includes(ch) && depth > 0) depth--
  }
  return depth > 0
}

/** The labels of a candidate match. Groups 1/2 belong to the wrapped branch, 3/4 to
 *  the bare one, and exactly one pair is defined per match. */
function labelsOf(match: RegExpMatchArray): string {
  return (match[2] ?? match[4]) ?? ''
}

/** Every marker in `content` whose terminator is its own, in order.
 *
 * Clones the pattern per call, so the g-flag `lastIndex` hazard cannot reach a
 * caller. A refused candidate cannot hide an accepted one inside its span: the body
 * refuses a nested `[OPTION(S):`, so no candidate ever contains another head. */
export function findOptionMarkers(content: string): RegExpMatchArray[] {
  const out: RegExpMatchArray[] = []
  for (const m of content.matchAll(new RegExp(OPTION_MARKER_RE))) {
    if (!labelsHaveUnmatchedOpener(labelsOf(m))) out.push(m)
  }
  return out
}

/** The LAST accepted marker, which is the one whose options a turn offers. */
export function findLastOptionMarker(content: string): RegExpMatchArray | null {
  const all = findOptionMarkers(content)
  return all.length > 0 ? all[all.length - 1] : null
}

/** `content` with every accepted marker of BOTH kinds removed, refused candidates left in place.
 *
 * Refused text staying visible is the point: a candidate this declines is prose the
 * user should still see, and deleting it is the defect the check exists to prevent.
 *
 * BOTH kinds, because "keyed on one head" is a repeat defect rather than a hypothetical:
 * `"[OPTION-ACTIONS:"` does not start with `"[OPTIONS:"` — they diverge at `S` vs `-` — so a
 * site that strips only the content head passes the action marker through untouched while
 * looking correct. That shape was counted at three consumers (a hand-back probe, a substance
 * measure, and the search index). The action pass runs SECOND, matching `parseOptions`: each
 * pattern anchors on ending its own line, so removing one can let the other reach an anchor it
 * could not before. */
export function stripOptionMarkers(content: string): string {
  const parts: string[] = []
  let cursor = 0
  for (const m of findOptionMarkers(content)) {
    const start = m.index ?? 0
    parts.push(content.slice(cursor, start))
    cursor = start + m[0].length
  }
  parts.push(content.slice(cursor))
  return stripActionMarkers(parts.join(''))
}

/** The closing brackets the marker pattern accepts — ASCII plus the CJK lookalikes.
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

/** One Markdown wrapper character — mirrors OPTION_MARKER_RE's wrapper class. */
const WRAP_CHAR_RE = /[`*_]/

/** Start of a LINE-LEADING wrapper run abutting position `p` of `tail`, else `p`.
 *
 * The streaming counterpart to OPTION_MARKER_RE's optional leading-wrapper
 * group: a wrapped marker's head is located at its `[`, and cutting there
 * would leave the wrapper visible while the marker it belongs to is hidden.
 * The run must abut `p`, be at most 3 characters, and carry only indent before
 * it on the line — a mid-line wrapper belongs to prose (the completed regex
 * leaves it visible too, since its leading group is `^`-anchored) and a 4+ run
 * is not a wrapper. */
function wrapperStart(tail: string, p: number): number {
  let w = p
  while (w > 0 && p - w < 3 && WRAP_CHAR_RE.test(tail[w - 1])) w--
  if (w === p) return p
  return /^[ \t]*$/.test(tail.slice(0, w)) ? w : p
}

/** A head that is still being TYPED — every prefix of `[OPTIONS:` / `[OPTION:`,
 *  from the bare `[` up to the full head, spelled as nested optionals.
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
    // A closer with only trailing blanks after it DOES reach here since #9284:
    // parseOptions now declines a marker whose interior closer is neither matched
    // nor continuing (`[OPTIONS: Fix ]x logging | Skip]`), so its final closer
    // arrives with nothing after it. Cutting is still the right branch — this is
    // reached only under the isStreaming gate (AssistantMessage.tsx), so the frame
    // is transient and the sealed render shows the marker as written.
    const rest = closer < 0 ? '' : body.slice(closer + 1)
    const forming = closer < 0 || rest.trim() === '' || CONTINUES_LABELS_RE.test(rest)
    return forming ? cutAt(text, start + wrapperStart(tail, head)) : text
  }

  const open = tail.lastIndexOf('[')
  // Walk back over a wrapper run abutting the `[` (≤3): `` `[OPT `` / `**[OPT`
  // is a marker-to-be. A LINE-LEADING run is cut with the head — the completed
  // OPTION_MARKER_RE strips it too. A mid-line run after whitespace stays
  // visible while the head behind it is hidden, again mirroring the completed
  // regex, whose leading-wrapper group is `^`-anchored.
  let run = open
  while (run > 0 && open - run < 3 && WRAP_CHAR_RE.test(tail[run - 1])) run--
  const lineLeading = /^[ \t]*$/.test(tail.slice(0, run))
  // The canonical marker opens its own line; the same-line variant the regex
  // also accepts still has a space before the `[` (or before its wrapper run).
  // Requiring that boundary costs the marker nothing and takes every in-word
  // bracket (`arr[0`, a footnote ref, `arr**[` behind a glued run) out of
  // scope entirely.
  if (!lineLeading && !/\s/.test(tail[run - 1] ?? '')) return text
  const frag = tail.slice(open)
  const partial = PARTIAL_HEAD_UPPER_RE.test(frag) || PARTIAL_HEAD_LOWER_RE.test(frag)
  return partial ? cutAt(text, start + (lineLeading ? run : open)) : text
}

/**
 * TEST-ONLY view of the ACTION pattern's shape, as a STRING.
 *
 * A string, not a regex, for the reason the content pattern's own source export gives: the
 * patterns are `g`-flagged, so handing one out hands out mutable `lastIndex` state, and this
 * module's boundary test asserts structurally that nothing with a `lastIndex` escapes.
 * Production callers go through `matchActionMarkers` / `stripActionMarkers`.
 */
export const OPTION_ACTION_MARKER_PATTERN_SOURCE = OPTION_ACTION_MARKER_RE.source

/**
 * The flag set BOTH patterns carry, exported once so a parity pin has something to assert.
 *
 * The two heads sharing one flag set is a real property — a case-sensitivity or multiline
 * divergence between them is exactly the silent asymmetry the shared grammar exists to
 * prevent — but it cannot be read off a `.source` string. Pinning it here keeps the pin
 * possible without exporting a regex object.
 */
export const MARKER_PATTERN_FLAGS = OPTION_MARKER_RE.flags
