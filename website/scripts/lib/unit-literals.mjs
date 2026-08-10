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
])

/** Measurement units this UI renders. Symbols only. */
export const UNIT = /^(ms|s|m|h|d|B|KB|MB|GB|TB|kB|K|M|%|x)(?![a-zA-Z])/

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
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/** Is this node lexically inside a position whose value is CSS? */
function inCssContext(node) {
  for (let n = node; n; n = n.parent) {
    // style={{ ... }} or style="..."
    if (ts.isJsxAttribute(n) && CSS_JSX_ATTR.has(n.name.getText())) return true
    // { width: `...` }
    if (ts.isPropertyAssignment(n)) {
      const key = ts.isIdentifier(n.name) || ts.isStringLiteral(n.name) ? n.name.text : ''
      if (CSS_PROPERTY.has(key)) return true
    }
    // el.style.height = ... / setProperty('--x', ...)
    if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      if (/\.style\b/.test(n.left.getText())) return true
    }
    if (ts.isCallExpression(n)) {
      const callee = n.expression.getText()
      if (/setProperty$/.test(callee) || /useMotionTemplate$/.test(callee)) return true
    }
    // Tagged template: useMotionTemplate`...`
    if (ts.isTaggedTemplateExpression(n) && /MotionTemplate/.test(n.tag.getText())) return true
  }
  return false
}

/** Line numbers where a numeric span is glued to a unit literal. */
export function unitLiteralHits(file, source) {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits = new Set()
  const line = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1

  const visit = (node) => {
    // `${expr}UNIT`: the literal chunk that FOLLOWS an interpolation.
    if (ts.isTemplateExpression(node)) {
      const whole = node.getText(sf)
      if (!CSS_UNIT.test(whole) && !inCssContext(node)) {
        for (const span of node.templateSpans) {
          const text = span.literal.text
          // Allow one space between value and unit (`5 KB`), as CLDR does.
          if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(span.literal))
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
        if (inCssContext(cur)) continue
        const text = next.text
        if (CSS_UNIT.test(text)) continue
        if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(cur))
      }
    }
    // expr + 'UNIT'
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.PlusToken
      && ts.isStringLiteral(node.right)
      && !inCssContext(node)
      && !CSS_UNIT.test(node.right.text)
    ) {
      const text = node.right.text
      if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(node.right))
    }
    ts.forEachChild(node, visit)
  }

  visit(sf)
  return [...hits].sort((a, b) => a - b)
}
