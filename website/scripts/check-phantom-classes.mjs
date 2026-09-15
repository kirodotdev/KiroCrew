#!/usr/bin/env node
/**
 * check-phantom-classes.mjs — gate color utilities whose token does not exist.
 *
 * A Tailwind utility naming a token the theme never declared is not an error
 * anywhere: `tailwindcss` simply does not emit the class, the element renders
 * with no color, and nothing — not the build, not the type checker, not eslint —
 * says a word. `STATE_DOT.connected = 'bg-success'` in InstancesPanel shipped
 * like that: the palette declares `ok`, not `success`, so the "Connected" crew
 * had no status dot at all while the states that happened to use real tokens
 * (`bg-muted`, `bg-danger`) kept theirs. The bug reads as a coloring quirk,
 * which is why it survived review.
 *
 * ## Why this asks Tailwind instead of comparing against a token list
 *
 * The obvious gate — parse `theme.extend.colors` and diff the heads found in
 * source against it — manufactures false positives faster than it finds bugs.
 * Measured over this tree: 480 candidate heads outside the palette, of which 68
 * are real phantoms. The other 412 are correct code:
 *
 *   - composite utilities whose head is not the token: `border-l-accent`,
 *     `ring-offset-bg`, `divide-border`, `border-l-[3px]`;
 *   - the entire non-color half of these prefixes: `text-sm`, `border-none`,
 *     `outline-none`, `ring-inset`, `bg-transparent`, `bg-gradient-to-r`;
 *   - Tailwind's own palette: `text-red-500`.
 *
 * Every one of those IS emitted by Tailwind. So the question this gate asks is
 * not "is the head a token" but "does Tailwind emit this class" — which is
 * exactly the failure mode, needs no table of exceptions to maintain, and stays
 * correct when the config gains a token or a Tailwind upgrade changes what
 * resolves. The config is the input, never re-implemented: one compile of
 * `@tailwind utilities` against the real config, with every candidate handed to
 * it as content, and the emitted selectors are the answer.
 *
 * ## Two filters, and why a bare token is not a class
 *
 * The remaining noise is string literals that are not class lists at all. Two
 * cheap rules clear it without touching any real phantom, both derived from the
 * measured corpus rather than guessed:
 *
 *  1. A literal must LOOK like a class list: at least two whitespace-separated
 *     tokens, at least one of which Tailwind emits. This is what separates
 *     `"mt-1 text-[12px] text-warning"` from `slotMessages: { 'bg-slot': … }`,
 *     `data-testid="to-closed"`, `labels: ['from-row']` and
 *     `boxSizing: 'border-box'`. `from-` and `to-` are unavoidable collisions
 *     with English prepositions — 223 of the 362 measured false positives are
 *     lone-token test fixtures of exactly that shape.
 *     The cost is real and is stated here so it is a decision: a phantom that
 *     is the ONLY token in its literal is not reported. `STATE_DOT`'s
 *     `'bg-success'` is such a literal — which is why rule 3 exists.
 *  2. A literal carrying `;`, `{`, `}` or a `prop:` declaration head is CSS,
 *     not a class list (`FE_CSS`, the widget srcdoc bodies, inline transitions).
 *  3. A single-token literal is still checked when it sits in a CLASS-VALUED
 *     position — a `className`/`class` attribute, a `cn()`/`clsx()`/`cva()`
 *     argument, or a value in an object/array whose own name says it holds
 *     classes (`STATE_DOT`, `borderColor`, `…Cls`, `…Class`, `…ClassName`).
 *     That is the seam the original bug lived in, so a gate that cannot see it
 *     would not have caught the thing it exists to catch.
 *
 * ## Diff-scoped, with the backlog printed
 *
 * The tree carries a pre-existing backlog, so a whole-tree gate would charge it
 * to whoever pushed next. Enforcement reads only literals this change touched
 * (`PHANTOM_BASE_REF`), which is complete for regression — a phantom can only
 * reach main through a diff that wrote it. The whole-tree count is always
 * printed as a non-failing notice so the backlog stays visible.
 *
 * ## Usage
 *
 *     # enforce on what this branch touched (exit 1 on any violation)
 *     PHANTOM_BASE_REF=origin/main npm run lint:phantom-classes
 *
 *     # report the whole-tree backlog, enforce nothing (exit 0)
 *     npm run lint:phantom-classes
 *
 *     # self-test: one probe per rule family, assert each verdict
 *     npm run lint:phantom-classes -- --test
 *
 * ## Escape hatch
 *
 * A class this rule cannot resolve (one built by a plugin, or a name that is
 * deliberately not a utility) can opt out with a `phantom-class-ok` comment on
 * the line the literal starts. Use it sparingly, and say where the class comes
 * from.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import postcss from 'postcss'
import tailwindcss from 'tailwindcss'
import tailwindConfig from '../tailwind.config.js'

const WEBSITE = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const REPO_ROOT = join(WEBSITE, '..')
const SCAN_ROOT = 'website/src'
const MARKER = 'phantom-class-ok'
const GATE = 'phantom-classes gate'

/** Utility prefixes that take a COLOR token. Deliberately not every prefix:
 *  the gate's claim is about color tokens, and a wider net would need a wider
 *  justification than the measured corpus supports. */
const COLOR_PREFIX = /^(bg|text|border|ring|ring-offset|from|via|to|fill|stroke|outline|decoration|divide|placeholder|caret|accent|shadow)-/

/** Leading Tailwind variant segments, plus the `!` important marker that may
 *  follow them.
 *
 *  Three forms, and each was a hole while this only matched a bare word:
 *   - a named variant, optionally carrying an arbitrary argument: `hover:`,
 *     `data-[state=open]:`, `group-[&_tr]:`
 *   - a fully arbitrary variant, which starts with `[` rather than a word:
 *     `[&_tr]:border-warn`, `[&>*]:bg-warn`
 *   - `!` for important, which sits AFTER the variants: `hover:!bg-warn`
 *
 *  A form this does not strip leaves the token's head unrecognisable to
 *  `COLOR_PREFIX`, so the candidate is dropped and the phantom is never checked
 *  — a false negative, which is the one failure this gate must not have now that
 *  it replaces the per-file denylists. */
const VARIANTS = /^(?:(?:\[[^\]]*\]|[a-z0-9@][a-z0-9-]*(?:-\[[^\]]*\])?):)*!?/i

/** A word worth handing to the compile as content.
 *
 *  `!` and `[` are legal class starts (important, arbitrary variant), so
 *  excluding them would leave a REAL class absent from the oracle while the
 *  rules above still judge it — which reads as a phantom. Shared by both call
 *  sites deliberately: the oracle silently under-answering is invisible, and two
 *  copies of this filter is exactly how they drift apart. */
const ORACLE_WORD = /^[!a-z0-9@[]/i
const oracleWords = (words) =>
  words.filter((t) => ORACLE_WORD.test(t) && t.length < 80)

/** A CSS declaration head (`transition:`, `boxSizing:`) rather than a variant. */
const CSS_DECL = /(?:^|\s)[a-zA-Z-]+:\s/

/** Markup, not a class list: an attribute assignment or an element open tag.
 *  `kiroGhostAvatar.ts` builds SVG in a template literal, so `stroke-width="20`
 *  arrives looking exactly like a candidate class. No class list ever contains
 *  `="`. */
const MARKUP = /="|<[A-Za-z]/

/** Regex metacharacters that never occur in a class list. Tests store their
 *  assertion patterns as strings (`'…text-\\[11px\\]|justify-between…'`), and
 *  after unescaping those read as classes. `|`, `^`, `$` and `(?` do not. */
const REGEX_SOURCE = /[|^$]|\(\?/

/** A candidate that cannot be a class whatever the token is: a dangling alpha
 *  modifier (`border-l/`) or a prefix used as a string test
 *  (`c.startsWith('bg-')`). */
const INCOMPLETE_TOKEN = /[-/]$/

/** Test files are excluded, and that is a decision rather than convenience.
 *
 *  A test's class strings are assertion DATA — `toHaveClass('border-l-ok')`,
 *  `filter(c => c.startsWith('bg-'))`, a `not.toMatch(/…/)` pattern stored as a
 *  string — so they are noise by construction, and 5 of the last 6 false
 *  positives measured on this tree came from exactly there. The cost is that a
 *  phantom written only inside a test is not reported; measured over the tree
 *  that cost is zero real phantoms, because a class a test asserts on has to
 *  exist in the component for the test to pass. */
const EXCLUDED = /(?:^|\/)test\/|\.test\.tsx?$|\.spec\.tsx?$/

/** Syntactic positions where a lone string is never a class, checked against the
 *  text immediately preceding the literal on its own line.
 *
 *  Both cases are real and neither is distinguishable by content:
 *   - a comparison operand: `className={viewAnim === 'to-detail' ? … : …}` sits
 *     INSIDE a className expression, so the class-valued-context rule accepts it,
 *     but `'to-detail'` is a state value being tested, not a class.
 *   - an attribute name: `t.setAttribute('text-anchor', …)`, where a neighbouring
 *     line's `strokeColor` is enough to satisfy the context rule by itself. */
const NOT_A_CLASS_POSITION = /(?:===|!==|==|!=)\s*$|(?:set|get|remove|has)Attribute\(\s*$/

/** Names whose value is understood to hold class strings. */
const CLASS_VALUED = /\b(?:className|class|cn|clsx|cva|twMerge|classNames?|[A-Za-z_$][\w$]*(?:Cls|Class|ClassName|Color|COLOR|_DOT|Dot))\b/

// ---------------------------------------------------------------------------
// Source lexing
// ---------------------------------------------------------------------------

/** Every string literal / template static chunk in `src`, with its line.
 *
 * Hand-lexed rather than regexed: a `//` inside a string, a `'` inside a
 * comment, and a nested template inside `${…}` all change what counts as a
 * literal, and getting any of them wrong silently changes which code the gate
 * reads.
 *
 * Two structural details are load-bearing:
 *
 *  - Every static chunk of ONE template literal shares a `group`. A template is
 *    a single class list to Tailwind, so splitting it into chunks and judging
 *    each alone would turn `` `flex items-center ${gap} bg-surface` `` into a
 *    lone-token fragment and lose the corroboration that proves it is a class
 *    list. Chunks keep their own line for reporting; the group carries the
 *    evidence.
 *  - `${…}` bodies are skipped as literal TEXT but re-lexed as SOURCE, so a
 *    class string inside an interpolation (`${cond ? 'gap-2' : 'gap-1'}`) is
 *    still seen — with its own group, since those branches are alternatives.
 */
export function literals(src, lineOffset = 0, groupBase = 0) {
  const out = []
  let line = 1 + lineOffset
  let group = groupBase
  let i = 0
  const n = src.length
  const push = (value, startLine, g, quoteAt) => {
    if (!value.trim()) return
    // Text on the literal's own line up to its opening quote. Only meaningful
    // for a plain quoted string (a template chunk after an interpolation has no
    // single "preceding text"), which is exactly where the syntactic-position
    // rules apply.
    const before =
      quoteAt === undefined ? '' : src.slice(src.lastIndexOf('\n', quoteAt) + 1, quoteAt)
    out.push({ value, line: startLine, group: g, before })
  }
  const nextGroup = () => {
    group += 1
    return `${groupBase}.${group}`
  }
  while (i < n) {
    const c = src[i]
    const two = src.slice(i, i + 2)
    if (two === '//') {
      while (i < n && src[i] !== '\n') i += 1
      continue
    }
    if (two === '/*') {
      const end = src.indexOf('*/', i + 2)
      const stop = end < 0 ? n : end + 2
      for (; i < stop; i += 1) if (src[i] === '\n') line += 1
      continue
    }
    if (c === '"' || c === "'") {
      const startLine = line
      const quoteAt = i
      const quote = c
      i += 1
      let buf = ''
      while (i < n && src[i] !== quote) {
        if (src[i] === '\n') break // unterminated; bail rather than mis-lex
        if (src[i] === '\\') {
          // Keep the escaped character. Dropping it (i += 2 and nothing
          // appended) silently rewrites the literal: a regex source stored as a
          // string, `'text-\\[13px\\]'`, becomes `text-13px` — a token that is
          // not in the source, is not emitted by Tailwind, and is reported as a
          // phantom. That was 9 of this gate's findings, every one of them a
          // test's own assertion pattern.
          buf += src[i + 1] ?? ''
          i += 2
          continue
        }
        buf += src[i]
        i += 1
      }
      i += 1
      push(buf, startLine, nextGroup(), quoteAt)
      continue
    }
    if (c === '`') {
      const templateGroup = nextGroup()
      i += 1
      let buf = ''
      let startLine = line
      while (i < n && src[i] !== '`') {
        if (src[i] === '\\') {
          buf += src[i + 1] ?? ''
          i += 2
          continue
        }
        if (src.slice(i, i + 2) === '${') {
          push(buf, startLine, templateGroup)
          buf = ''
          // Capture the interpolation body at brace depth, then re-lex it as
          // source so literals inside it are not lost.
          const bodyStart = i + 2
          let depth = 1
          i += 2
          while (i < n && depth > 0) {
            if (src[i] === '{') depth += 1
            else if (src[i] === '}') depth -= 1
            if (depth > 0) i += 1
          }
          const body = src.slice(bodyStart, i)
          out.push(...literals(body, line - 1, group * 1000))
          for (const ch of body) if (ch === '\n') line += 1
          i += 1
          startLine = line
          continue
        }
        if (src[i] === '\n') line += 1
        buf += src[i]
        i += 1
      }
      i += 1
      push(buf, startLine, templateGroup)
      continue
    }
    if (c === '\n') line += 1
    i += 1
  }
  return out
}

/** Strip punctuation glued to the end of a class by prose, not by the class.
 *
 * `border-warn/40.` in an error message is a real token plus a sentence-final
 * period. But `text-[12px]` ends in a bracket that IS the class: stripping it
 * unconditionally turns every arbitrary value into a phantom (`text-[12px`),
 * which is how this gate reported its first false positives. So a closer is
 * only punctuation when it is unbalanced.
 */
function trimPunctuation(raw) {
  let token = raw
  for (;;) {
    const last = token.at(-1)
    if (last && '.,;:!?\'"`'.includes(last)) {
      token = token.slice(0, -1)
      continue
    }
    const pair = last === ')' ? '(' : last === ']' ? '[' : null
    if (pair) {
      const opens = token.split(pair).length - 1
      const closes = token.split(last).length - 1
      if (closes > opens) {
        token = token.slice(0, -1)
        continue
      }
    }
    return token
  }
}

/** The candidate class tokens in one literal value, each with its OWN line.
 *
 * A template literal's static chunk can span lines, and the chunk only knows
 * where it started. Attributing every token in it to that first line is wrong in
 * the one place it matters most: enforcement is diff-scoped, so a phantom added
 * on the fifth line of a multiline `className={\`…\`}` would be reported against
 * the first — an untouched line — and skipped. Counting the newlines ahead of
 * each token costs one pass and makes the reported line the line the token is
 * actually on. */
function tokensOf(value, startLine = 1) {
  const out = []
  let line = startLine
  let at = 0
  for (const m of value.matchAll(/\S+/g)) {
    for (; at < m.index; at += 1) if (value[at] === '\n') line += 1
    const token = trimPunctuation(m[0])
    if (!token || INCOMPLETE_TOKEN.test(token)) continue
    if (!COLOR_PREFIX.test(token.replace(VARIANTS, ''))) continue
    out.push({ token, line })
  }
  return out
}

/** Whether `value` is plausibly a class list rather than CSS or markup. */
function looksLikeCss(value) {
  return (
    /[;{}]/.test(value) ||
    CSS_DECL.test(value) ||
    MARKUP.test(value) ||
    REGEX_SOURCE.test(value)
  )
}

// ---------------------------------------------------------------------------
// Ask Tailwind which classes it emits
// ---------------------------------------------------------------------------

/** The subset of `candidates` Tailwind actually emits a rule for.
 *
 * One compile for the whole run. The real config is passed through untouched
 * except for `content`, so every token, plugin and theme extension the app has
 * is in force — the gate never re-implements a resolution rule it could ask
 * about.
 */
export async function emittedClasses(candidates) {
  if (candidates.length === 0) return new Set()
  const raw = [...new Set(candidates)].join(' ')
  const config = { ...tailwindConfig, content: [{ raw, extension: 'html' }] }
  const { css } = await postcss([tailwindcss(config)]).process(
    '@tailwind utilities;',
    { from: undefined },
  )
  const emitted = new Set()
  // Selectors arrive escaped: `.hover\:bg-ok:hover`, `.bg-warn\/10`,
  // `.border-l-\[3px\]`. Read to the first UNescaped delimiter, then unescape.
  for (const m of css.matchAll(/\.((?:\\(?:[0-9a-fA-F]{1,6}\s?|.)|[^\s.,:>+~()[\]{}"'])+)/g)) {
    emitted.add(unescapeSelector(m[1]))
  }
  return emitted
}

/** Reverse CSS identifier escaping in a class selector.
 *
 * Two forms, and only handling the second is a silent oracle failure: CSS
 * escapes a comma as the NUMERIC form `\2c ` (hex codepoint plus one optional
 * trailing whitespace), not as `\,`. A naive `\\(.)` unescape turns
 * `.bg-\[color-mix\(in_srgb\2c var\(--warn\)_12%\2c transparent\)\]` into a
 * string containing a literal `2c `, which then fails to match the class in
 * source — so every arbitrary value carrying a comma reads as a phantom. That
 * was 20 of this gate's first 136 "findings", all of them correct code.
 */
export function unescapeSelector(sel) {
  return sel.replace(/\\(?:([0-9a-fA-F]{1,6})\s?|(.))/g, (_, hex, ch) =>
    hex ? String.fromCodePoint(parseInt(hex, 16)) : ch,
  )
}

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------

/** Candidate literals in `src`, each already filtered and marker-checked. */
export function candidatesIn(src) {
  const lines = src.split('\n')
  const out = []
  for (const entry of literals(src)) {
    if (looksLikeCss(entry.value)) continue
    if ((lines[entry.line - 1] || '').includes(MARKER)) continue
    const tokens = tokensOf(entry.value, entry.line)
    if (tokens.length === 0) continue
    out.push({ ...entry, tokens })
  }
  return out
}

/** Every whitespace-separated word of every non-CSS literal, for the oracle. */
function allWords(src) {
  const out = []
  for (const { value } of literals(src)) {
    if (looksLikeCss(value)) continue
    for (const raw of value.trim().split(/\s+/)) {
      const token = trimPunctuation(raw)
      if (token) out.push(token)
    }
  }
  return out
}

/** Violations in one file, given the emitted-class oracle.
 *
 * Evidence is gathered per GROUP (one plain string, or one whole template
 * literal): a multi-token class list needs at least one emitted sibling to prove
 * it is a class list at all, and that sibling may sit in a different static
 * chunk of the same template. A group that is a single token has no siblings, so
 * it needs a class-valued position instead — the seam
 * `STATE_DOT: { connected: 'bg-success' }` sits in.
 */
export function violationsIn(src, emitted, rel) {
  const lines = src.split('\n')
  const entries = candidatesIn(src)
  const groupWords = new Map()
  for (const entry of literals(src)) {
    if (looksLikeCss(entry.value)) continue
    const words = entry.value
      .trim()
      .split(/\s+/)
      .map(trimPunctuation)
      .filter(Boolean)
    groupWords.set(entry.group, [...(groupWords.get(entry.group) || []), ...words])
  }
  const out = []
  for (const { tokens, line, group, before } of entries) {
    const bad = tokens.filter((t) => !emitted.has(t.token))
    if (bad.length === 0) continue
    const words = groupWords.get(group) || []
    if (words.length > 1) {
      if (!words.some((t) => emitted.has(t))) continue
    } else {
      // A lone token: only a class-valued position makes it a class. Look at
      // the line itself and the two above, which covers `KEY: 'bg-success',`
      // inside a declared map and a `className={cond ? 'x' : 'y'}` split over
      // lines, without reaching far enough to borrow an unrelated name.
      //
      // The literal's OWN start line, not the token's: a lone token is a
      // single-word literal, so the two are the same, and the surrounding code
      // is what this window is reading.
      if (NOT_A_CLASS_POSITION.test(before)) continue
      const window = lines.slice(Math.max(0, line - 3), line).join('\n')
      if (!CLASS_VALUED.test(window)) continue
    }
    for (const t of bad) out.push({ path: rel, line: t.line, token: t.token })
  }
  return out
}

// ---------------------------------------------------------------------------
// Filesystem + git
// ---------------------------------------------------------------------------

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (/\.tsx?$/.test(p) && !EXCLUDED.test(relative(REPO_ROOT, p))) out.push(p)
  }
  return out
}

const scanFiles = () => walk(join(REPO_ROOT, SCAN_ROOT))

function git(args) {
  return execFileSync('git', args, { cwd: REPO_ROOT, encoding: 'utf-8' })
}

function diffBase(base) {
  try {
    return git(['merge-base', base, 'HEAD']).trim()
  } catch {
    return base // shallow CI clone may have no merge base
  }
}

function changedPaths(from) {
  let out
  try {
    out = git(['diff', '--name-only', '-z', '--diff-filter=d', from])
  } catch (err) {
    console.log(
      `::error::${GATE}: cannot diff against ${from} — the base commit is not ` +
        'present. Fetch it before running, or unset PHANTOM_BASE_REF to report ' +
        `whole-tree counts without enforcing.\n${err.message}`,
    )
    process.exit(1)
  }
  return out
    .split('\0')
    .filter(
      (p) => p && p.startsWith(SCAN_ROOT) && /\.tsx?$/.test(p) && !EXCLUDED.test(p),
    )
}

/** 1-based line numbers this change adds to `path`. */
export function hunkTouchedLines(diff) {
  const touched = new Set()
  for (const raw of diff.split('\n')) {
    if (!raw.startsWith('@@')) continue
    const m = /\+(\d+)(?:,(\d+))?/.exec(raw)
    if (!m) continue
    const start = Number(m[1])
    const count = m[2] === undefined ? 1 : Number(m[2])
    // A deletion-only hunk reads `+N,0`: `range(N, N)` is empty, so a naive
    // read reports nothing as touched. Deleting the line that carried the real
    // token is exactly how a phantom gets introduced, so anchor at N.
    if (count === 0) touched.add(start)
    else for (let i = start; i < start + count; i += 1) touched.add(i)
  }
  return touched
}

const touchedLines = (from, path) =>
  hunkTouchedLines(
    git(['diff', '--unified=0', '--no-color', '--text', from, '--', path]),
  )

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------

const REMEDY =
  `\nThe class is not emitted, so the element renders with NO color. Check the ` +
  `token against \`theme.extend.colors\` in website/tailwind.config.js — the ` +
  `palette is ok / warn / danger / info / accent / muted / bg-* / border* / ` +
  `card / aim / clarify / diff-*, so a "success" is \`ok\` and a "warning" is ` +
  `\`warn\`. Alpha and -subtle/-fg forms exist only where the config declares ` +
  `them.\nNOTE: a CSS variable existing is NOT enough — \`--panel\`, ` +
  `\`--panel-strong\` and \`--border-hover\` are defined in every theme but are ` +
  `absent from \`theme.extend.colors\`, so \`bg-panel\` and ` +
  `\`border-border-hover\` emit nothing. Add the token to the config, or use a ` +
  `declared one.\nIf the class is genuinely produced elsewhere, put a ` +
  `\`${MARKER}\` comment on the literal's line and say where it comes from.`

function report(violations, { enforcing, scope }) {
  if (violations.length === 0) {
    console.log(
      `${GATE}: every color utility in ${scope} names a token the theme ` +
        'declares \u2713',
    )
    return 0
  }
  if (enforcing) {
    console.log(
      `::error::${GATE}: ${violations.length} color utilit(y|ies) this change ` +
        'touched name a token the theme does not declare, so Tailwind emits no ' +
        'rule and they render with no color:',
    )
  } else {
    console.log(
      `::notice::${GATE} report: ${violations.length} pre-existing color ` +
        'utilit(y|ies) in the tree name a token the theme does not declare. Not ' +
        'enforced here; only literals a change touches are gated.',
    )
  }
  const shown = enforcing ? 200 : 40
  for (const v of violations.slice(0, shown)) {
    console.log(`  ${v.path}:${v.line}  ${v.token}`)
  }
  if (violations.length > shown) {
    console.log(`  ... and ${violations.length - shown} more`)
  }
  if (enforcing) console.log(REMEDY)
  return enforcing ? 1 : 0
}

async function scanAll() {
  const files = scanFiles().map((abs) => ({
    abs,
    rel: relative(REPO_ROOT, abs),
    src: readFileSync(abs, 'utf-8'),
  }))
  const candidates = []
  for (const f of files) candidates.push(...allWords(f.src))
  const emitted = await emittedClasses(oracleWords(candidates))
  return { files, emitted }
}

async function reportTree() {
  const { files, emitted } = await scanAll()
  const violations = []
  for (const f of files) violations.push(...violationsIn(f.src, emitted, f.rel))
  return report(violations, { enforcing: false, scope: 'the whole tree' })
}

async function enforceDiff(base) {
  const from = diffBase(base)
  const paths = changedPaths(from)
  const { emitted } = await scanAll()
  const violations = []
  const unreadable = []
  for (const path of paths) {
    let src
    try {
      src = readFileSync(join(REPO_ROOT, path), 'utf-8')
    } catch {
      unreadable.push(path)
      continue
    }
    const touched = touchedLines(from, path)
    for (const v of violationsIn(src, emitted, path)) {
      if (touched.has(v.line)) violations.push(v)
    }
  }
  if (unreadable.length) {
    console.log(
      `::error::${GATE}: cannot read these changed files as UTF-8, so their ` +
        `color utilities were never checked: ${unreadable.join(', ')}`,
    )
    return 1
  }
  return report(violations, {
    enforcing: true,
    scope: `the literals touched since ${base}`,
  })
}

// ---------------------------------------------------------------------------
// Self-test — one probe per rule family
// ---------------------------------------------------------------------------

const PROBES = [
  {
    name: 'phantom in a multi-token class list is caught',
    src: 'const a = <div className="mt-1 text-[12px] text-warning" />',
    caught: ['text-warning'],
  },
  {
    name: 'real token in the same shape is not',
    src: 'const a = <div className="mt-1 text-[12px] text-warn" />',
    caught: [],
  },
  {
    name: 'phantom alone in a class-valued map IS caught (the original bug)',
    src: "const STATE_DOT = {\n  connected: 'bg-success',\n}",
    caught: ['bg-success'],
  },
  {
    name: 'lone token in a non-class position is not a class',
    src: "const s = { slotMessages: { 'bg-slot': [] } }",
    caught: [],
  },
  {
    name: 'a navigation testid is not a class',
    src: 'const a = <button data-testid="to-closed" />',
    caught: [],
  },
  {
    name: 'a CSS declaration string is not a class list',
    src: "el.style.transition = 'border-color 150ms, box-shadow 150ms'",
    caught: [],
  },
  {
    name: 'a CSS body in a template literal is not a class list',
    src: 'const CSS = `.x { border-right: 1px solid red; text-transform: none; }`',
    caught: [],
  },
  {
    name: 'composite side utilities resolve and are not phantoms',
    src: 'const a = <div className="border-l-2 border-l-accent ring-offset-bg" />',
    caught: [],
  },
  {
    name: 'arbitrary values resolve and are not phantoms',
    src: 'const a = <div className="border-l-[3px] bg-[#fff] text-[13px]" />',
    caught: [],
  },
  {
    name: 'a variant-prefixed phantom is caught through its variant',
    src: 'const a = <div className="rounded hover:text-fg" />',
    caught: ['hover:text-fg'],
  },
  {
    name: 'a variant-prefixed real token is not',
    src: 'const a = <div className="rounded hover:text-text" />',
    caught: [],
  },
  {
    name: 'an important-marked phantom is caught through the `!`',
    src: 'const a = <div className="rounded hover:!bg-warning" />',
    caught: ['hover:!bg-warning'],
  },
  {
    name: 'an important-marked real token is not',
    src: 'const a = <div className="rounded hover:!bg-warn" />',
    caught: [],
  },
  {
    name: 'a phantom behind an arbitrary variant is caught',
    src: 'const a = <div className="rounded [&_tr]:border-warning" />',
    caught: ['[&_tr]:border-warning'],
  },
  {
    name: 'a real token behind an arbitrary variant is not',
    src: 'const a = <div className="rounded [&_tr]:border-warn" />',
    caught: [],
  },
  {
    name: 'a phantom on a later line of a multiline template is reported there',
    src: 'const a = <div className={`flex items-center\n  gap-2\n  text-warning`} />',
    caught: ['text-warning'],
    lines: [3],
  },
  {
    name: 'a var that exists without a token is still a phantom',
    src: 'const a = <div className="rounded border border-border bg-panel p-3" />',
    caught: ['bg-panel'],
  },
  {
    name: 'trailing sentence punctuation is not part of the class',
    src: "const msg = 'use the token form instead: border-warn/40.'",
    caught: [],
  },
  {
    name: 'a comparison operand inside className is not a class',
    src: "const a = <div className={anim === 'to-detail' ? 'wl-to-detail' : ''} />",
    caught: [],
  },
  {
    name: 'an attribute name is not a class, even beside a *Color identifier',
    src: "t.setAttribute('fill', safeColor(el.strokeColor, INK))\nt.setAttribute('text-anchor', 'start')",
    caught: [],
  },
  {
    name: 'SVG markup in a template literal is not a class list',
    src: 'const svg = `<circle stroke-width="20" stroke-linecap="round" fill="none"/>`',
    caught: [],
  },
  {
    name: 'a regex pattern stored as a string is not a class list',
    src: "const re = 'px-4 py-2.5 text-[11px]|justify-between border-t/'",
    caught: [],
  },
  {
    name: 'a class-name prefix used as a string test is not a class',
    src: "const bgs = cls.split(' ').filter((c) => c.startsWith('bg-'))",
    caught: [],
  },
  {
    name: 'a valid token with an off-scale alpha step is still a phantom',
    src: 'const a = <div className="ml-auto rounded-full bg-accent/12 text-accent" />',
    caught: ['bg-accent/12'],
  },
  {
    name: 'the escape-hatch marker suppresses a real phantom',
    src: 'const a = <div className="mt-1 text-warning" /> // phantom-class-ok: from a plugin',
    caught: [],
  },
  {
    name: 'a phantom inside a comment is not live code',
    src: '// const a = <div className="mt-1 text-warning" />\nconst b = 1',
    caught: [],
  },
  {
    name: 'a phantom in a template literal static chunk is caught',
    src: 'const c = `flex items-center ${gap} bg-surface`',
    caught: ['bg-surface'],
  },
  {
    name: 'an interpolation body is skipped as text but re-read as source',
    src: 'const c = `flex ${cond ? "gap-2 text-warning" : "gap-1"} items-center`',
    caught: ['text-warning'],
  },
]

async function selfTest() {
  const candidates = []
  for (const p of PROBES) candidates.push(...allWords(p.src))
  const emitted = await emittedClasses(oracleWords(candidates))
  const disagree = []
  for (const p of PROBES) {
    const got = violationsIn(p.src, emitted, 'probe.tsx')
    const want = p.caught
    if (got.map((v) => v.token).join(',') !== want.join(',')) {
      disagree.push(
        `${p.name}: expected [${want.join(', ')}], got ` +
          `[${got.map((v) => v.token).join(', ')}]`,
      )
      continue
    }
    // A probe may also pin WHERE the finding lands. Only the multiline-template
    // probes need it, and only they can regress it, but a wrong line silently
    // disables diff-scoped enforcement rather than failing anything.
    if (p.lines && got.map((v) => v.line).join(',') !== p.lines.join(',')) {
      disagree.push(
        `${p.name}: expected line(s) [${p.lines.join(', ')}], got ` +
          `[${got.map((v) => v.line).join(', ')}]`,
      )
    }
  }
  // Floor: the oracle must be live. If the compile returned nothing, every
  // probe above would "agree" by reporting each token as a phantom, or none.
  if (!emitted.has('text-warn') || emitted.has('text-warning')) {
    disagree.push(
      'oracle: expected the compile to emit text-warn and not text-warning ' +
        `(emitted ${emitted.size} classes)`,
    )
  }
  if (disagree.length) {
    console.log(
      `::error::${GATE} self-test: ${disagree.length} probe(s) disagree with ` +
        'the rules:',
    )
    for (const d of disagree) console.log(`  ${d}`)
    return 1
  }
  console.log(`${GATE} self-test: ${PROBES.length} probes agree \u2713`)
  return 0
}

// ---------------------------------------------------------------------------

async function main(argv) {
  if (argv.includes('--test')) return selfTest()
  const base = (process.env.PHANTOM_BASE_REF || '').trim()
  if (!base) return reportTree()
  // Print the whole-tree backlog FIRST, then enforce on the diff. Both, not
  // either: CI always supplies a base, so an `enforce if base else report`
  // split would mean the backlog notice never prints anywhere a human reads it.
  await reportTree()
  return enforceDiff(base)
}

// Run only as a program. Without this guard `import`ing the module to unit-test
// `literals` / `violationsIn` would execute the whole scan and then call
// `process.exit`, so the importing test would die mid-collection.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(await main(process.argv.slice(2)))
}
