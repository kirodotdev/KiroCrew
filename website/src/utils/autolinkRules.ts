/**
 * Bare-token autolink registry.
 *
 * GFM autolinks anything carrying a scheme. It cannot know that in a given
 * organisation a bare token is an address — a ticket key, a change-review id.
 * A downstream edition registers that vocabulary here; the core registers none,
 * so a stock build renders exactly as it would with no seam.
 *   registerAutolinkRules([{
 *     id: 'acme-ticket',
 *     pattern: /\bTICKET-\d+\b/g,
 *     href: 'https://tickets.example.com/{match}',
 *   }])
 *
 * Registered at module load, before `App` mounts. Not reactive.
 */
import { reportSeamCollision } from '../apps/seamCollision'
import { safeHttpUrl } from '../lib/safeUrl'

export interface AutolinkRule {
  /** Stable key for de-dup across re-entrant registration (e.g. HMR). */
  id: string
  /**
   * The token shape. Matched against message PROSE only — never inside code,
   * an existing link, raw HTML, or math (see `remarkAutolinkRules`).
   *
   * Anchor it with `\b` or an explicit boundary class. An unanchored pattern
   * matches inside longer words, which is how `CR-1` ends up linked out of the
   * middle of `INCR-12`.
   */
  pattern: RegExp
  /**
   * Destination template. `{match}` is substituted with the matched token,
   * percent-encoded, so a token cannot introduce a scheme, userinfo, host or
   * query separator.
   *
   * Must be an absolute `http:` or `https:` URL, carrying no userinfo, with
   * `{match}` outside the authority. All three are checked ONCE, at
   * registration: encoding confines a token to a single path, query or fragment
   * segment, so there is no per-match outcome a re-check could differ on.
   */
  href: string
  /**
   * Longest text-node slice this rule scans; longer nodes are skipped for
   * this rule only. Set on operator-config rules (`CONFIG_RULE_MAX_SUBJECT`),
   * because a transcript is attacker-shaped input and the subject length is
   * the one factor of the scan cost registration-time checks cannot bound.
   * Unset on edition rules — a reviewed vocabulary scans whole nodes.
   */
  maxSubject?: number
}

const AUTOLINK_RULES: AutolinkRule[] = []

const MATCH_TOKEN = '{match}'

/** A high surrogate with no low after it, or a low with no high before it. */
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g

/** Substitute the matched token into a template, percent-encoded. */
function expand(template: string, match: string): string {
  // A stream cut mid-pair leaves an unpaired surrogate, which the encoder
  // rejects with a URIError; U+FFFD is the platform's own substitute.
  const text = match.replace(LONE_SURROGATE, '\uFFFD')
  return template.split(MATCH_TOKEN).join(encodeURIComponent(text))
}

/**
 * Validate a destination template, returning it or `null` if unusable.
 *
 * Two DIFFERENT canaries must yield the same origin. That is what makes one
 * check sufficient: it rejects a placeholder sitting in the authority, where a
 * token could otherwise steer the host, and leaves only path, query and
 * fragment positions — which percent-encoding cannot escape from.
 */
function normaliseHref(template: string): string | null {
  if (typeof template !== 'string' || !template.includes(MATCH_TOKEN)) return null
  const a = safeHttpUrl(expand(template, 'aaa'))
  const b = safeHttpUrl(expand(template, 'bbb'))
  if (!a || !b) return null
  if (new URL(a).origin !== new URL(b).origin) return null
  return template
}

/**
 * Editor-facing validity check for one operator-typed URL template — the
 * EXACT acceptance rule registration applies (`normaliseHref`), so the
 * Settings inline flag and the registry can never disagree: a template
 * carrying userinfo or a `{match}` in the authority is flagged where it is
 * typed instead of saving-then-never-linkifying.
 */
export function configUrlTemplateOk(template: string): boolean {
  return normaliseHref(template) !== null
}

/** The normalized destination for one matched token. */
export function autolinkHref(rule: AutolinkRule, match: string): string {
  return new URL(expand(rule.href, match)).href
}

/**
 * Normalise a rule's pattern into one that is safe to scan with in a loop:
 * global, non-sticky, and not matching the empty SUBJECT.
 *
 * That last check is a floor, not a guarantee: a pattern can still yield a
 * zero-width match against real content (`/(?=x)/`), which the scan handles by
 * abandoning the rule. Returns `null` when the pattern cannot be made safe, in
 * which case the caller reports a collision-style failure and drops the rule.
 */
function normalisePattern(p: RegExp): RegExp | null {
  if (p.sticky) return null
  // Empty-match test on a deliberately empty subject: a pattern that matches ''
  // matches it here too, and one that cannot will simply miss.
  const probe = new RegExp(p.source, p.flags.replace(/[gy]/g, ''))
  if (probe.test('')) return null
  const flags = p.flags.includes('g') ? p.flags : `${p.flags}g`
  return new RegExp(p.source, flags)
}

/**
 * Register one or more bare-token autolink rules. A duplicate `id` is ignored
 * and reported, so re-entrant registration stays idempotent; an invalid rule is
 * reported and dropped rather than taking the render down with it.
 */
export function registerAutolinkRules(rules: AutolinkRule[]): void {
  for (const r of rules) {
    if (AUTOLINK_RULES.some(existing => existing.id === r.id)) {
      reportSeamCollision('autolinkRules', `rule ${r.id} already registered; ignoring duplicate`)
      continue
    }
    const pattern = normalisePattern(r.pattern)
    if (!pattern) {
      reportSeamCollision(
        'autolinkRules',
        `rule ${r.id} has an unusable pattern (sticky, or matches the empty string); ignoring`,
      )
      continue
    }
    const href = normaliseHref(r.href)
    if (!href) {
      reportSeamCollision(
        'autolinkRules',
        `rule ${r.id} has an unusable href template (needs {match} in the path, query or fragment of an absolute http(s) URL without userinfo); ignoring`,
      )
      continue
    }
    AUTOLINK_RULES.push({ ...r, pattern, href })
  }
}

/** All registered rules, in registration order (earlier rules win an overlap). */
export function getAutolinkRules(): readonly AutolinkRule[] {
  return CONFIG_RULES.length === 0 ? AUTOLINK_RULES : [...AUTOLINK_RULES, ...CONFIG_RULES]
}

/** Drop every registered rule. Test-only; the app never unregisters. */
export function resetAutolinkRulesForTest(): void {
  AUTOLINK_RULES.length = 0
  CONFIG_RULES.length = 0
}

/* ── Operator-config rules (dashboard.link_patterns) ──────────────────────── */

/**
 * Rules the OPERATOR configures at runtime, as opposed to the vocabulary an
 * edition registers at module load. Kept in their own array so a config save
 * can replace the whole set: `registerAutolinkRules` is deliberately
 * append-only with id de-dup (module-load semantics), which cannot express
 * "this rule was edited or removed" — swapping a separate set can, which is
 * what makes the Settings editor live without a reload.
 *
 * Edition rules stay ahead of config rules in `getAutolinkRules()`, so on an
 * overlapping span the registration-order-wins contract keeps the edition's
 * vocabulary authoritative.
 */
const CONFIG_RULES: AutolinkRule[] = []

/**
 * Structurally reject the catastrophic-backtracking class: a QUANTIFIED GROUP
 * whose body itself contains a quantifier or an alternation, or a quantified
 * backreference. Exponential backtracking needs ambiguity under repetition —
 * `(a+)+`, `(a|aa)+`, `(\w+)\1+` — and every spelling of that ambiguity in a
 * regex source is one of those shapes. An empirical probe cannot close this
 * class (any bounded subject ladder has a knee some growth rate slips past:
 * `(a|aa)+` is mild at 24 chars and seconds at 40), so the shape is refused
 * outright and the probe below remains only as belt-and-braces for what a
 * syntactic scan cannot see.
 *
 * Deliberately conservative: a SAFE alternation under a quantifier (e.g.
 * `(?:proj|ops)+`, disjoint branches) is also refused, because deciding
 * branch-overlap is the hard half of the problem. Unquantified groups keep
 * full expressiveness — `(?:proj|ops)-\d+` passes — which covers work-item
 * vocabularies, this feature's domain.
 */
function hasCatastrophicShape(source: string): boolean {
  // Group stack: one flag per open group — "body contains a quantifier or
  // alternation". Depth 0 is the pattern top level, where a bare alternation
  // without repetition is harmless.
  const stack: boolean[] = [false]
  let prev: 'atom' | 'group-close-safe' | 'group-close-risky' | 'other' = 'other'
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]
    if (ch === '\\') {
      // A quantified backreference repeats an ambiguous-width capture.
      if (/\d/.test(source[i + 1] ?? '') && /[+*{?]/.test(source[i + 2] ?? '')) return true
      i += 1 // escaped char: literal atom
      prev = 'atom'
      continue
    }
    if (ch === '[') {
      // Character class: contents are literal, including | + * {.
      let j = i + 1
      if (source[j] === '^') j += 1
      // NO leading-] skip: that rule is POSIX. In ECMAScript `[]` and `[^]`
      // close immediately (empty class / any-char), so skipping a leading `]`
      // would swallow what follows as class contents and hide a catastrophic
      // suffix from this scan — `[^](a|aa)+$` measured ~100ms at 28 chars.
      // A literal `]` member must be written `\]`, which the escape branch
      // below already walks correctly.
      while (j < source.length && source[j] !== ']') {
        if (source[j] === '\\') j += 1
        j += 1
      }
      i = j
      prev = 'atom'
      continue
    }
    if (ch === '(') {
      stack.push(false)
      // `(?:` `(?=` `(?!` `(?<name>` etc: the ? here is group syntax, not a
      // quantifier — skip it so the quantifier branch below never sees it.
      if (source[i + 1] === '?') i += 1
      prev = 'other'
      continue
    }
    if (ch === ')') {
      const risky = stack.pop() ?? false
      if (stack.length === 0) stack.push(false) // unbalanced source: stay safe
      if (risky) stack[stack.length - 1] = true // riskiness propagates outward
      prev = risky ? 'group-close-risky' : 'group-close-safe'
      continue
    }
    if (ch === '|') {
      if (stack.length > 1) stack[stack.length - 1] = true
      prev = 'other'
      continue
    }
    if (ch === '+' || ch === '*' || ch === '{' || ch === '?') {
      // `{` only quantifies when it opens `{n}`/`{n,}`/`{n,m}`.
      if (ch === '{' && !/^\{\d+(,\d*)?\}/.test(source.slice(i))) {
        prev = 'atom'
        continue
      }
      if (prev === 'group-close-risky') return true
      if (stack.length > 0) stack[stack.length - 1] = true
      if (ch === '{') i = source.indexOf('}', i)
      // `prev` keeps its group-close state so `){2}?` (lazy) re-checks the
      // same closed group rather than reading `?` as a fresh quantifier.
      continue
    }
    prev = 'atom'
  }
  return false
}

/**
 * Refuse a pattern whose quantifiers open more backtracking choice than the
 * budget. Every VARIABLE-WIDTH quantifier is a pump — a choice dimension the
 * engine can rebalance while backtracking: `*`, `+` and `{n,}` (unbounded),
 * but equally `?` and `{n,m}` with n≠m, because a CHAIN of bounded pumps
 * explodes the same way (`a{0,1000}` five times over a 100-char subject is
 * ~10^8 width splits — the range never binds, the subject does; only `{n}`
 * is width-fixed). One pump is the accepted baseline: a single choice
 * dimension costs the scan-restart quadratic the node cap already bounds.
 * Each FURTHER pump multiplies that cost by its effective width range, so
 * the product of every pump's range beyond the single widest one must stay
 * within `CONFIG_RULE_PUMP_BUDGET` — worst accepted cost is budget × the
 * capped quadratic (16 × 4M steps ≈ tens of ms), and the width a pump can
 * actually express is clamped at the node cap before it enters the product.
 * Width-fixed `{n}` opens no choice of its own, but beside any pump it
 * multiplies the verify cost of every split that pump opens, so it pays
 * into the same product (and never rides free). Zero-pump patterns are
 * width-deterministic and exempt regardless of fixed runs.
 * `\b[A-Z]{1,10}-\d+\b` (10 beyond the free `\d+`) and `\bID\d{2,}x?\b`
 * (2 beyond) pass; `{0,1000}` beside any other pump is refused, and so is
 * `a+a{100}z` (100-wide fixed verify × unbounded splits).
 */
function pumpBudgetExceeded(source: string): boolean {
  const factors: number[] = []
  const fixedWidths: number[] = []
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]
    if (ch === '\\') {
      i += 1 // escaped char: literal atom; its quantifier is read next round
      continue
    }
    if (ch === '[') {
      let j = i + 1
      if (source[j] === '^') j += 1
      // NO leading-] skip: that rule is POSIX. In ECMAScript `[]` and `[^]`
      // close immediately (empty class / any-char), so skipping a leading `]`
      // would swallow what follows as class contents and hide a catastrophic
      // suffix from this scan — `[^](a|aa)+$` measured ~100ms at 28 chars.
      // A literal `]` member must be written `\]`, which the escape branch
      // below already walks correctly.
      while (j < source.length && source[j] !== ']') {
        if (source[j] === '\\') j += 1
        j += 1
      }
      i = j
      continue
    }
    if (ch === '(' && source[i + 1] === '?') {
      i += 1 // group-syntax `?`, never a quantifier
      if (source[i + 1] === '<' && source[i + 2] !== '=' && source[i + 2] !== '!') {
        const close = source.indexOf('>', i + 1)
        if (close !== -1) i = close // named-group label chars are not atoms
      }
      continue
    }
    // Effective width range of one quantifier, clamped at the node cap: a
    // range wider than the longest scannable subject cannot express more
    // splits than the subject allows.
    if (ch === '*' || ch === '+') {
      factors.push(CONFIG_RULE_MAX_SUBJECT + 1)
      continue
    }
    if (ch === '?') {
      factors.push(2)
      continue
    }
    if (ch === '{') {
      const m = /^\{(\d+)(,(\d*))?\}/.exec(source.slice(i))
      if (m) {
        const lo = Number(m[1])
        // Width-fixed demand above the cap: no backtracking choice, but the
        // linear cost repeats at every scan-restart position and is invisible
        // to both the pump product and the ≤24-char probe ladder.
        if (lo > CONFIG_RULE_MAX_FIXED_WIDTH) return true
        if (m[2] === undefined) {
          // `{n}`: width-fixed, not a pump — but a verify-cost multiplier
          // once any pump exists (see below).
          if (lo > 1) fixedWidths.push(lo)
        } else if (m[3] === '') {
          factors.push(CONFIG_RULE_MAX_SUBJECT + 1) // `{n,}`: unbounded
        } else {
          const range = Math.min(Number(m[3]) - lo, CONFIG_RULE_MAX_SUBJECT)
          if (range > 0) factors.push(range + 1)
        }
        i += m[0].length - 1
      }
    }
  }
  // Zero pumps = width-deterministic: no backtracking choice exists, so
  // fixed-width runs cost one linear pass and `{40}:{40}` SHA-pair shapes
  // stay accepted. The moment ANY pump exists, every fixed-width run beside
  // it multiplies the verify cost of EACH split the pump opens — `a+a{100}z`
  // over a flooded subject re-verifies the 100-wide tail at every split,
  // a quadratic spent inside ONE synchronous exec, before the per-document
  // budget can intervene (found as the round-20 `{n}`-adjacency bypass). So
  // fixed widths enter the same product the pumps pay into; only the single
  // widest PUMP rides free, never a fixed run.
  if (factors.length === 0) return false
  if (factors.length <= 1 && fixedWidths.length === 0) return false
  // The single widest pump rides for free (dimension 1 is the baseline);
  // every other pump multiplies the budget.
  factors.sort((a, b) => b - a)
  let product = 1
  for (let k = 1; k < factors.length; k++) {
    product *= factors[k]
    if (product > CONFIG_RULE_PUMP_BUDGET) return true
  }
  for (const w of fixedWidths) {
    product *= w
    if (product > CONFIG_RULE_PUMP_BUDGET) return true
  }
  return false
}

/**
 * Ceiling on the backtracking choice multiplier a config pattern's pumps may
 * open beyond its single widest one. See `pumpBudgetExceeded`.
 */
const CONFIG_RULE_PUMP_BUDGET = 16

/**
 * Ceiling on the FIXED width a brace quantifier may demand (`{n}`, and the
 * `n` floor of `{n,}` / `{n,m}`). Width-fixed repetition opens no
 * backtracking choice, but its LINEAR cost still scales with `n` at every
 * scan-restart position — `[a-z]{1000}[a-z]{999}b` passes the pump budget
 * (zero pumps) yet costs ~5ms per 2000-char node, and only the per-document
 * budget contains it. No real link-token vocabulary repeats an atom
 * hundreds of times, so the cap costs nothing while keeping a config rule's
 * single-scan cost in the same envelope the probe ladder actually measures.
 */
const CONFIG_RULE_MAX_FIXED_WIDTH = 100

/**
 * Longest text-node slice an operator-config rule will scan. With the
 * pump-budget gate above, one rule's cost on one node is
 * bounded by the scan-restart quadratic: 2000² = 4M steps, single-digit
 * milliseconds. Without a cap, a transcript is attacker-shaped input — a
 * pasted log can hand the scan a 100K-character node. Edition rules are a
 * reviewed vocabulary and keep scanning whole nodes.
 */
const CONFIG_RULE_MAX_SUBJECT = 2000

/** Mixed transcript-shaped subjects every probe includes — token and
 * whitespace shapes a single-character run cannot represent. Kept lowercase /
 * single-char: this is machine input, and capitalised prose here would read
 * as a UI string to the i18n source scan. */
const REDOS_PROBE_ALPHABETS: readonly string[] = ['a1', ' ', 'proj-1234 ']

/** Cap on distinct pattern-derived probe characters. Patterns are ≤300 chars
 * and real work-item vocabularies use a handful of letters; 8 keeps the probe
 * matrix (chars x rungs) trivially cheap for linear patterns. */
const REDOS_PROBE_MAX_DERIVED_CHARS = 8

/**
 * Characters the pattern can actually consume, read from its own source: a
 * backtracking explosion needs a long input run the pattern can pump, and
 * every pumpable character is spelled in the pattern — as a literal, a class
 * member, or a shorthand class. Probing runs of exactly these characters is
 * what closes the fixed-alphabet gap, where `(b+)+$` sailed past subjects
 * built only from `a`: no hand-picked alphabet can cover an operator's regex,
 * but the pattern names its own.
 */
function probeCharsFor(source: string): string[] {
  const chars = new Set<string>()
  // Shorthand classes and the dot: one representative per class they admit.
  if (/\\[wSD]|\./.test(source)) {
    chars.add('a')
    chars.add('_')
  }
  if (/\\[wdS]|\./.test(source)) chars.add('1')
  if (/\\[sWD]|\./.test(source)) chars.add(' ')
  // Letters, digits and underscores spelled in the pattern (class members and
  // literals alike; Unicode included). Metacharacters cannot start a pumpable
  // run and escapes were handled above, so this simple scan is sufficient.
  for (const ch of source.match(/[\p{L}\p{N}_]/gu) ?? []) {
    if (chars.size >= REDOS_PROBE_MAX_DERIVED_CHARS) break
    chars.add(ch)
  }
  return [...chars]
}

/** Subject lengths, smallest first. Exponential backtracking is already
 * unmistakable at these sizes (`(a+)+$` costs ~2^n steps: ~65K at 16, ~16M at
 * 24) while a linear pattern clears every rung in microseconds. The ladder
 * ascends ONLY while under budget, so the largest rung a pathological pattern
 * can ever reach is the one after its first slow rung — that caps the worst
 * single `exec()` at roughly 16x the budget trip point, a few hundred ms,
 * paid once at registration. Subjects large enough to make one exec()
 * effectively non-terminating (2^2048 …) must never appear here: `exec()` is
 * not interruptible, so no wall-clock check can bound a single call.
 */
const REDOS_PROBE_LENGTHS: readonly number[] = [8, 12, 16, 20, 24]

/** Total probe budget per rule. Linear scans finish all rungs in well under
 * a millisecond; anything that trips this is refused. */
const REDOS_PROBE_BUDGET_MS = 20

/**
 * Reject a pattern whose backtracking grows super-linearly. Walks the length
 * ladder over the pattern's OWN characters (see probeCharsFor) plus the mixed
 * transcript shapes, re-checking the wall clock between EVERY exec — the
 * ladder's small subjects are what keep each individual exec bounded (see
 * REDOS_PROBE_LENGTHS); the clock decides between rungs, never inside one.
 * Each subject carries a failing `!` tail so an almost-match must backtrack.
 */
function exceedsScanBudget(pattern: RegExp): boolean {
  const scan = new RegExp(pattern.source, pattern.flags)
  const alphabets = [...probeCharsFor(pattern.source), ...REDOS_PROBE_ALPHABETS]
  const start = performance.now()
  for (const length of REDOS_PROBE_LENGTHS) {
    for (const alphabet of alphabets) {
      const subject = `${alphabet.repeat(Math.ceil(length / alphabet.length)).slice(0, length)}!`
      scan.lastIndex = 0
      scan.exec(subject)
      if (performance.now() - start > REDOS_PROBE_BUDGET_MS) return true
    }
  }
  return false
}

/**
 * Replace the operator-config rule set. Each entry passes the SAME validation
 * as a registered rule (pattern normalised, href template origin-stable with
 * `{match}` outside the authority) plus a bounded execution probe, because —
 * unlike an edition's reviewed vocabulary — these patterns are user input:
 * a catastrophic-backtracking pattern (`(a+)+$`) must be refused before it
 * can meet transcript text on the render path. An entry that fails is dropped
 * silently — this is user input arriving on every config save, and the
 * Settings editor already flags an invalid pattern inline, so a
 * seam-collision report would only turn a typo into alarm noise.
 */
export function setConfigAutolinkRules(rules: Array<{ pattern: string; url: string }>): void {
  CONFIG_RULES.length = 0
  for (let i = 0; i < rules.length; i++) {
    const entry = rules[i]
    let raw: RegExp
    try {
      raw = new RegExp(entry.pattern, 'g')
    } catch {
      continue
    }
    const pattern = normalisePattern(raw)
    if (!pattern) continue
    const href = normaliseHref(entry.url)
    if (!href) continue
    // Structural rejection first (backreferences whole, then the catastrophic
    // class, then any over-budget pump chain), then the empirical ladder as
    // belt-and-braces for shapes a syntax scan cannot see. What survives is
    // single-pump, and the per-node subject cap turns its scan cost into a
    // hard ceiling.
    if (hasBackreference(pattern.source)) continue
    if (hasCatastrophicShape(pattern.source)) continue
    if (pumpBudgetExceeded(pattern.source)) continue
    if (exceedsScanBudget(pattern)) continue
    CONFIG_RULES.push({
      id: `config-link-pattern-${i}`,
      pattern,
      href,
      maxSubject: CONFIG_RULE_MAX_SUBJECT,
    })
  }
}

/**
 * Whether a config pattern source would be refused by the structural safety
 * gates (`hasCatastrophicShape`, the pump-width budget). The
 * Settings editor calls this to tell the operator WHY a rule does not fire —
 * registration drops unsafe rules silently, and a rule that saves fine but
 * never linkifies anything is otherwise indistinguishable from a bad regex.
 * The timing probe is deliberately not repeated here: it needs a compiled
 * pattern and real wall time, and every shape it refuses that these two
 * cannot see is degenerate rather than typable by accident.
 */
/**
 * Any backreference disqualifies a config pattern. Matching a backreference
 * forces the engine to re-try every capture split, which backtracks
 * polynomially without any quantifier nesting the shape gates can see, and
 * the wall-clock probe's synthesized subjects do not reliably trigger it:
 * `(a+)\1b` passes both gates yet measures ~125ms against a 2000-char
 * subject — 50 rules of it freeze rendering for seconds. Nothing is lost by
 * refusing: substitution is `{match}`-only, so a group can only ever serve
 * grouping, never reuse.
 */
function hasBackreference(source: string): boolean {
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]
    if (ch === '\\') {
      const next = source[i + 1] ?? ''
      if (/[1-9]/.test(next)) return true // \1–\9 (octal escapes are a syntax error under the u flag)
      if (next === 'k' && source[i + 2] === '<') return true // \k<name>
      i += 1 // any other escape: literal atom
      continue
    }
    if (ch === '[') {
      // Character class: `\1` inside is never a backreference in ECMAScript.
      // Same lexing as the shape gates: no leading-] skip, escapes walked.
      let j = i + 1
      if (source[j] === '^') j += 1
      while (j < source.length && source[j] !== ']') {
        if (source[j] === '\\') j += 1
        j += 1
      }
      i = j
    }
  }
  return false
}

export function configPatternUnsafe(source: string): boolean {
  return hasBackreference(source) || hasCatastrophicShape(source) || pumpBudgetExceeded(source)
}

/**
 * Aggregate wall-clock budget for OPERATOR-CONFIG rule execution across one
 * document render. The registration ladder bounds each rule's cost on ONE
 * subject, but cost multiplies across rules × text nodes: 50 rules that each
 * pass the per-scan gate at ~16ms freeze the main thread for ~8s against ten
 * 2KB subjects — measured identically through the prose pass and through
 * whole-match chip scans, so BOTH drain this one pool. The TOP-LEVEL
 * MarkdownRenderer re-arms it once per message render: one message assembles
 * MANY remark trees (`useBlockAssembler` splits on fences), so a per-tree
 * rearm would multiply the pool by the block count — 50 accepted rules ×
 * fence-heavy message = multi-second freeze (the round-21 bypass). The remark
 * plugin and the inline-code chips only DRAIN what the message's pool has
 * left. Once spent, remaining config-rule work
 * degrades to plain text — linkification is cosmetic, and that degradation
 * is the entire failure mode. Edition rules are a reviewed first-party
 * vocabulary and are not metered.
 */
const CONFIG_SCAN_BUDGET_MS = 50
let configBudgetPerDocMs = CONFIG_SCAN_BUDGET_MS
let configBudgetLeftMs = CONFIG_SCAN_BUDGET_MS

/** Re-arm the pool; the top-level MarkdownRenderer calls this once per message render. */
export function rearmConfigScanBudget(): void {
  configBudgetLeftMs = configBudgetPerDocMs
}

export function configScanBudgetExhausted(): boolean {
  return configBudgetLeftMs <= 0
}

/** Subtract one timed scan from the pool. */
export function drainConfigScanBudget(elapsedMs: number): void {
  configBudgetLeftMs -= elapsedMs
}

/**
 * Test seam, same convention as `resetAutolinkRulesForTest`: overrides the
 * per-document budget (pass `undefined` to restore the default). Not part of
 * the rendering API — production always runs the default.
 */
export function setConfigScanBudgetForTest(ms?: number): void {
  configBudgetPerDocMs = ms ?? CONFIG_SCAN_BUDGET_MS
  configBudgetLeftMs = configBudgetPerDocMs
}

/**
 * Resolve a WHOLE string against the OPERATOR-CONFIG rules: the entire text
 * must be one match (`PROJ-123`, not `run PROJ-123 now`). This is the
 * inline-code chip's form — `inlineCode` is opaque to `remarkAutolinkRules`
 * by design, and the chip only trades click-to-copy for a link when the span
 * IS the work item, so the chip stays atomic. Edition rules (`maxSubject`
 * unset) are excluded: the chip exists for the config feature, and widening
 * a shipped edition id's chip from copy to open-in-new-tab is that
 * vocabulary's own call to make. Scans drain the same per-document budget as
 * the prose pass — inline-code spans are transcript text too, so an
 * exhausted pool degrades the chip to its copy-only form rather than letting
 * spans re-run every rule outside the meter. Returns the destination href,
 * or null.
 */
export function wholeMatchAutolinkHref(text: string): string | null {
  for (const rule of getAutolinkRules()) {
    if (rule.maxSubject === undefined) continue // edition rule: chip behavior unchanged
    if (text.length > rule.maxSubject) continue
    // Metered per RULE, not per call: one call over 50 rules costs up to
    // 50 ladder-bounded scans, which is exactly the multiplication the
    // budget exists to stop.
    if (configScanBudgetExhausted()) return null
    const scanStart = performance.now()
    rule.pattern.lastIndex = 0
    const m = rule.pattern.exec(text)
    drainConfigScanBudget(performance.now() - scanStart)
    if (m && m.index === 0 && m[0].length === text.length) return autolinkHref(rule, m[0])
  }
  return null
}
