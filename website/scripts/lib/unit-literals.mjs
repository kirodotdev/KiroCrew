/**
 * Number+unit literal matcher: the shared definition.
 *
 * Extracted verbatim from `src/i18n/unitLiterals.test.ts` so the repo-wide scan can
 * run as a standalone gate while the matcher's own unit tests stay in vitest. Same
 * move `scripts/lib/qa-checks.mjs` made, and for the same reason: a gate that must
 * run outside vitest needs its predicate importable from plain Node, and a second
 * copy would drift from the assertions that prove it works.
 *
 * The logic below is unchanged from the test it came from. That is deliberate: the
 * extraction is a refactor, and equivalence is verified file-by-file rather than
 * assumed.
 */

import { readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

import ts from 'typescript'

/** The seam is where a unit is legitimately turned into text. */
export const SEAM = new Set(['i18n/format.ts'])

/**
 * Machine formats whose output is parsed back. Localizing these breaks the round
 * trip, so they are exempt BY PATH with the parser named. Any addition here owes
 * the same citation.
 */
export const ROUND_TRIP = new Map([
  ['utils/cronUtils.tsx', 'wire shape read back by parseEveryFromSchedule in components/WeekGrid.tsx'],
  ['utils/pasteTokens.ts', 'token read back by PASTE_TOKEN_REGEX in the same file'],
  ['utils/tz.ts', 'UTC±HH:MM offset token, and en-US pins that derive cron day-of-week numbers'],
  ['components/fileChangeChipsCss.ts', 'CSS animation durations parsed by the browser stylesheet parser, not read as text'],
  ['lib/kiroGhostAvatar.ts', 'CSS animation durations/keyframes inside the generated SVG <style>, parsed by the browser stylesheet parser, not read as text'],
])

/** Measurement units this UI renders. Symbols only. */
export const UNIT = /^(ms|s|m|h|d|B|KB|MB|GB|TB|kB|K|M|%|x)(?![a-zA-Z])/

/**
 * The fast path: can this file hold a finding at all, judged from raw text?
 *
 * The scan parses every in-scope file to find sites in a few dozen of them, and the
 * parse is ~95% of its runtime. So reject a file from its raw text when it provably
 * cannot contain any of the three shapes, and parse only what survives. This is a
 * pure early-out: the matcher below is unchanged and still decides every finding.
 *
 * ## Why it cannot hide a finding
 *
 * All three shapes need a literal chunk whose text starts with an optional space
 * then a unit. In raw source such a chunk is preceded by:
 *
 *  - `}` -- a template continuation (the chunk after an interpolation is a
 *    TemplateMiddle/Tail token, which always opens with `}`), and equally JSX text
 *    following an expression, since the JsxText begins where the JsxExpression's `}`
 *    ends. `RAW_BRACE`.
 *  - a quote, for `expr + 'm'`, with only whitespace between, since `+` binds its
 *    operand directly. `RAW_CONCAT`.
 *
 * Both read the unit vocabulary out of `UNIT` itself rather than restating it, so
 * adding a unit cannot make the fast path narrower than the matcher.
 *
 * That leaves the two ways raw text and a cooked literal legitimately disagree, and
 * `RAW_DIFFERS` admits those files without reasoning about them at all:
 *
 *  - An escape. A unit can be spelled `\u006d`, `%` as `\x25`, or `m` as the
 *    identity escape `\m`, and a backslash before a newline continues the line and
 *    cooks to nothing, shifting what "first character" means. Any backslash admits
 *    the file: enumerating which escapes can cook to a unit character is exactly
 *    the kind of reasoning this clause exists to avoid, and a stray backslash in a
 *    literal is rare enough that the admission costs almost nothing.
 *  - A comment between `+` and its operand, which `RAW_CONCAT`'s `\s*` cannot cross.
 *
 * JSX text needs no such clause: it carries no escapes, and TypeScript does not
 * decode HTML entities into `JsxText.text` (`{x}&#37;` stays `&#37;`), so its raw
 * and cooked forms are the same string.
 *
 * The matcher's own vitest file pins this with a case per clause that IS a real
 * finding and is admitted by that clause alone, so removing a clause fails loudly
 * rather than going quietly blind.
 */
const UNIT_TAIL = UNIT.source.replace(/^\^/, '')
const RAW_BRACE = new RegExp(`\\}[ ]?${UNIT_TAIL}`)
const RAW_CONCAT = new RegExp(`\\+\\s*['"][ ]?${UNIT_TAIL}`)
const RAW_DIFFERS = /\\|\+\s*\/[/*]/

export function mayHoldUnitLiteral(source) {
  return RAW_BRACE.test(source) || RAW_CONCAT.test(source) || RAW_DIFFERS.test(source)
}

/** Units that only ever mean CSS. A literal containing one is a CSS value. */
export const CSS_UNIT = /\d\s*(px|r?em|vh|vw|vmin|vmax|fr|deg|ch|pt|cm|mm|dvh|svh)\b|\bcalc\(/

/** JSX attributes whose value is a CSS length, directly or one hop downstream. */
const CSS_JSX_ATTR = new Set(['style', 'w', 'h', 'width', 'height', 'size', 'delay', 'offset'])

/** Object keys that are CSS properties. */
const CSS_PROPERTY = new Set([
  'width', 'height', 'top', 'left', 'right', 'bottom', 'inset', 'transform', 'transition',
  'animation', 'animationDelay', 'animationDuration', 'transitionDuration', 'transitionDelay',
  'maxWidth', 'minWidth', 'maxHeight', 'minHeight', 'gridTemplateColumns', 'gridTemplateRows',
  'aspectRatio', 'boxShadow', 'padding', 'margin', 'lineHeight', 'fontSize', 'gap', 'flexBasis',
  'strokeDasharray', 'strokeWidth', 'fontWeight', 'borderRadius', 'font', 'filter', 'clipPath',
  'backgroundSize', 'backgroundPosition', 'translate', 'scale', 'rotate', 'offsetDistance',
])

/**
 * The scanned population, as a predicate on a path relative to `src`.
 *
 * `walk()` applies the structural half of this while traversing. The diff-scoped
 * gates get their paths from git instead, so they need the whole predicate in one
 * place, and it has to be the SAME one. A path the ceiling counts but the diff
 * gates skip would be a silent exemption that nothing reports.
 */
export function inScope(rel) {
  if (!/\.tsx?$/.test(rel) || /\.test\.tsx?$/.test(rel)) return false
  const parts = rel.split('/')
  if (parts.includes('node_modules') || parts[0] === 'locales') return false
  return !SEAM.has(rel) && !ROUND_TRIP.has(rel)
}

export function walk(dir, out = []) {
  // `withFileTypes` answers directory-or-file from the one `readdir` syscall the
  // traversal already makes, instead of a `stat` per entry -- a thousand extra
  // syscalls over a tree this size. A symlink is the one entry kind a Dirent cannot
  // classify (it reports the link, not the target), so those alone still get a
  // `stat`, which keeps the population identical to the pre-Dirent walk.
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'locales') continue
    const full = join(dir, entry.name)
    const isDir = entry.isSymbolicLink() ? statSync(full).isDirectory() : entry.isDirectory()
    if (isDir) walk(full, out)
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full)
  }
  return out
}

/**
 * Is this node lexically inside a position whose value is CSS?
 *
 * The ancestor chain is passed in, innermost first, rather than followed through
 * `node.parent`. Parent pointers are not free: asking `createSourceFile` for them
 * costs about as much again as the parse itself, and this is the only consumer of
 * them here. The visitor already knows the chain it descended, so it hands that over
 * instead. `sf` is passed to every `getText` for the same reason -- the no-argument
 * form finds its source file by walking `.parent`.
 */
function inCssContext(chain, sf) {
  for (const n of chain) {
    // style={{ ... }} or style="..."
    if (ts.isJsxAttribute(n) && CSS_JSX_ATTR.has(n.name.getText(sf))) return true
    // { width: `...` }
    if (ts.isPropertyAssignment(n)) {
      const key = ts.isIdentifier(n.name) || ts.isStringLiteral(n.name) ? n.name.text : ''
      if (CSS_PROPERTY.has(key)) return true
    }
    // el.style.height = ... / setProperty('--x', ...)
    if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      if (/\.style\b/.test(n.left.getText(sf))) return true
    }
    if (ts.isCallExpression(n)) {
      const callee = n.expression.getText(sf)
      if (/setProperty$/.test(callee) || /useMotionTemplate$/.test(callee)) return true
    }
    // Tagged template: useMotionTemplate`...`
    if (ts.isTaggedTemplateExpression(n) && /MotionTemplate/.test(n.tag.getText(sf))) return true
  }
  return false
}

/** Does this literal chunk start with a unit? One space is allowed, as CLDR does. */
function startsWithUnit(text) {
  return UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))
}

/**
 * Line numbers where a numeric span is glued to a unit literal.
 *
 * Ordered cheap-test-first throughout. The unit check reads a string already on the
 * node; the two CSS checks re-scan source text (`getText`) and walk the ancestor
 * chain. Since a site is a finding only if ALL of them agree, asking the cheap one
 * first is free and leaves the expensive pair running on the handful of candidates
 * rather than on every template, JSX element and `+` in the repo.
 */
export function unitLiteralHits(file, source) {
  // Cheap raw-text rejection before the parse; provably a superset of what the walk
  // below can match, so this only ever skips files with nothing to find.
  if (!mayHoldUnitLiteral(source)) return []

  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, false, ts.ScriptKind.TSX)
  const hits = new Set()
  const line = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1

  /** Ancestors of the node being visited, outermost first; see `inCssContext`. */
  const ancestry = []
  /** The chain `inCssContext` wants: `inner` (when given) then self, then upwards. */
  const chainFor = (inner) => {
    const chain = inner ? [inner] : []
    for (let i = ancestry.length - 1; i >= 0; i--) chain.push(ancestry[i])
    return chain
  }

  const visit = (node) => {
    ancestry.push(node)

    // `${expr}UNIT`: the literal chunk that FOLLOWS an interpolation.
    if (ts.isTemplateExpression(node)) {
      if (node.templateSpans.some((span) => startsWithUnit(span.literal.text))
        && !CSS_UNIT.test(node.getText(sf))
        && !inCssContext(chainFor(), sf)) {
        for (const span of node.templateSpans) {
          if (startsWithUnit(span.literal.text)) hits.add(line(span.literal))
        }
      }
    }
    // <span>{expr}UNIT</span>: a JSX expression followed by JSX text.
    if (ts.isJsxElement(node) || ts.isJsxFragment(node)) {
      const kids = node.children
      for (let i = 0; i < kids.length - 1; i++) {
        const cur = kids[i]
        const next = kids[i + 1]
        if (!ts.isJsxExpression(cur) || !ts.isJsxText(next)) continue
        const text = next.text
        if (!startsWithUnit(text)) continue
        if (CSS_UNIT.test(text)) continue
        if (inCssContext(chainFor(cur), sf)) continue
        hits.add(line(cur))
      }
    }
    // expr + 'UNIT'
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.PlusToken
      && ts.isStringLiteral(node.right)
      && startsWithUnit(node.right.text)
      && !CSS_UNIT.test(node.right.text)
      && !inCssContext(chainFor(), sf)
    ) {
      hits.add(line(node.right))
    }
    ts.forEachChild(node, visit)

    ancestry.pop()
  }

  visit(sf)
  return [...hits].sort((a, b) => a - b)
}
