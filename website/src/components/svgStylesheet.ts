/**
 * The parts of an SVG artifact's own CSS that reach only into the drawing.
 *
 * SvgShadow (components/SvgShadow.tsx) draws the SVG in a shadow root, which keeps
 * most selectors to the drawing, but the page still reaches into it: `:host` and
 * its kin select the dashboard-owned host and its ancestors, and the page's
 * custom properties inherit into the drawing, where a conditional (`if()`,
 * `style()`, `attr()` re-reading an attribute as CSS, and whatever CSS adds next)
 * can test them and fetch a remote image as the signal. So the CSS is kept by
 * allowlist, not by naming what to block:
 *
 * - a declaration, and every other attribute value (SVG reads `fill="..."` and
 *   its kin as CSS), keeps only the functions in `DRAWING_FUNCTIONS`; any other
 *   function drops the whole declaration or attribute. `var()` is not on the
 *   list: its fallback, or an empty value, tests whether the page defines a
 *   custom property;
 * - a selector keeps only the pseudo-classes in `DRAWING_PSEUDO_CLASSES` and no
 *   pseudo-element, and a top-level `&` (which can match the host) drops it;
 * - only the rule kinds below are kept, so `@import`, `@property`, `@page`,
 *   `@scope`, `@container` and their like go.
 *
 * The check reads tokens, not raw text: comments, strings and unquoted `url()`
 * are skipped as the CSS tokenizer skips them, and a function is a name followed
 * straight by `(`. Text holding a backslash goes whole, because a custom property
 * keeps its CSS escapes as typed (`\69 f(` is `if(`), and so does text with a
 * string cut off by a newline. With no parser for
 * constructable stylesheets the whole stylesheet goes.
 */
const DRAWING_FUNCTIONS = new Set([
  'url', 'calc', 'min', 'max', 'clamp',
  'rgb', 'rgba', 'hsl', 'hsla', 'hwb', 'lab', 'lch', 'oklab', 'oklch', 'color', 'color-mix',
  'linear-gradient', 'radial-gradient', 'conic-gradient',
  'repeating-linear-gradient', 'repeating-radial-gradient', 'repeating-conic-gradient',
  'matrix', 'matrix3d', 'translate', 'translatex', 'translatey', 'translatez', 'translate3d',
  'scale', 'scalex', 'scaley', 'scalez', 'scale3d', 'rotate', 'rotatex', 'rotatey', 'rotatez', 'rotate3d',
  'skew', 'skewx', 'skewy', 'perspective',
  'blur', 'brightness', 'contrast', 'drop-shadow', 'grayscale', 'hue-rotate', 'invert', 'opacity', 'saturate', 'sepia',
  'inset', 'circle', 'ellipse', 'polygon', 'path',
  'cubic-bezier', 'steps', 'local', 'format',
])
const DRAWING_PSEUDO_CLASSES = new Set([
  'hover', 'active', 'focus', 'focus-visible', 'focus-within', 'empty',
  'not', 'is', 'where', 'has',
  'first-child', 'last-child', 'only-child', 'nth-child', 'nth-last-child',
  'first-of-type', 'last-of-type', 'only-of-type', 'nth-of-type', 'nth-last-of-type',
])
const groupRules = new Set(['CSSMediaRule', 'CSSSupportsRule', 'CSSLayerBlockRule'])
const leafRules = new Set(['CSSKeyframesRule', 'CSSFontFaceRule', 'CSSLayerStatementRule', 'CSSNamespaceRule', 'CSSNestedDeclarations'])

// `closed` marks an unquoted `url(...)`, read with its `)` as one token.
type Token = { kind: 'name' | 'function' | 'delim'; value: string; start: number; closed?: boolean }

/** `text` with A to Z lower-cased and every other character left as it is. */
function asciiLower(text: string): string {
  let out = ''
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)
    out += code >= 65 && code <= 90 ? String.fromCharCode(code + 32) : text[i]
  }
  return out
}
/** A CSS name character: a to z, A to Z, 0 to 9, `_`, `-`, or anything at or past U+0080. */
function isNameChar(char: string): boolean {
  const code = char.charCodeAt(0)
  return (code >= 97 && code <= 122) || (code >= 65 && code <= 90) || (code >= 48 && code <= 57)
    || code === 95 || code === 45 || code >= 0x80
}
/** Whether the name run at `i` starts a CSS identifier: a letter, `_`, U+0080 or past, or `-` and one of those or `-`. */
function startsIdentifier(text: string, i: number): boolean {
  const isStart = (char: string | undefined) => {
    if (char === undefined) return false
    const code = char.charCodeAt(0)
    return (code >= 97 && code <= 122) || (code >= 65 && code <= 90) || code === 95 || code >= 0x80
  }
  return isStart(text[i]) || (text[i] === '-' && (text[i + 1] === '-' || isStart(text[i + 1])))
}
const isNewline = (char: string | undefined) => char === '\n' || char === '\r' || char === '\f'
const isSpace = (char: string | undefined) => char === ' ' || char === '\t' || isNewline(char)

/** CSS text as names, functions and single-character delimiters, or null when it holds a backslash or a bad string. */
function tokenize(text: string): Token[] | null {
  if (text.includes('\\')) return null
  const tokens: Token[] = []
  let i = 0
  while (i < text.length) {
    const char = text[i]
    if (char === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2)
      i = end < 0 ? text.length : end + 2
    } else if (char === '"' || char === "'") {
      i++
      while (i < text.length && text[i] !== char && !isNewline(text[i])) i++
      // A newline ends a string as a bad string, which the browser recovers from
      // in its own way: like a backslash, it fails the whole text.
      if (isNewline(text[i])) return null
      i++
    } else if (isNameChar(char)) {
      let end = i
      while (end < text.length && isNameChar(text[end])) end++
      const name = asciiLower(text.slice(i, end))
      // CSS makes a function only from an identifier followed straight by `(`:
      // after `#` the name is a hash, after `@` an at-keyword, and one led by a
      // digit (or `-` and a digit) a number, so their `(` opens a plain block.
      const prev = text[i - 1]
      if (text[end] === '(' && prev !== '#' && prev !== '@' && startsIdentifier(text, i)) {
        const token: Token = { kind: 'function', value: name, start: i }
        tokens.push(token)
        end++
        let next = end
        while (isSpace(text[next])) next++
        // An unquoted `url(` is one token up to its `)`. Only a well-formed one is
        // read so: when the span holds a quote, a `(` or inner whitespace (a bad
        // url), the browser and this reading could split it differently, so the
        // whole text fails.
        if (name === 'url' && text[next] !== '"' && text[next] !== "'") {
          const close = text.indexOf(')', next)
          if (close < 0) return null
          let last = close
          while (last > next && isSpace(text[last - 1])) last--
          for (let k = next; k < last; k++) {
            const c = text[k]
            if (c === '"' || c === "'" || c === '(' || isSpace(c)) return null
          }
          end = close + 1
          token.closed = true
        }
      } else {
        tokens.push({ kind: 'name', value: name, start: i })
      }
      i = end
    } else {
      tokens.push({ kind: 'delim', value: char, start: i })
      i++
    }
  }
  return tokens
}

/** Whether a CSS value, such as an SVG presentation attribute, uses only drawing functions. */
export function svgOwnValue(value: string): boolean {
  const tokens = tokenize(value)
  return !!tokens && tokens.every(token => token.kind !== 'function' || DRAWING_FUNCTIONS.has(token.value))
}

/** The declarations of a block (`a: b; c: d`) that use only drawing functions, as block text. */
export function svgOwnDeclarations(block: string): string {
  const tokens = tokenize(block)
  if (!tokens) return ''
  const declarations: string[] = []
  let depth = 0
  let start = 0
  const end = (at: number) => {
    const text = block.slice(start, at).trim()
    if (text && svgOwnValue(text)) declarations.push(text)
    start = at + 1
  }
  for (const token of tokens) {
    if ((token.kind === 'function' && !token.closed) || (token.kind === 'delim' && '([{'.includes(token.value))) depth++
    else if (token.kind === 'delim' && ')]}'.includes(token.value)) depth = Math.max(0, depth - 1)
    else if (token.kind === 'delim' && token.value === ';' && depth === 0) end(token.start)
  }
  end(block.length)
  const kept = declarations.map(text => `${text};`).join(' ')
  // The joined text is checked again, so no piece can change how another reads.
  return svgOwnValue(kept) ? kept : ''
}

function ownSelector(selector: string, nested: boolean): boolean {
  const tokens = tokenize(selector)
  if (!tokens) return false
  return tokens.every((token, i) => {
    if (token.kind === 'delim' && token.value === '&') return nested
    if (token.kind !== 'delim' || token.value !== ':') return true
    const next = tokens[i + 1]
    return next?.kind !== 'delim' && next?.start === token.start + 1 && DRAWING_PSEUDO_CLASSES.has(next.value)
  })
}

type RuleParent = { readonly cssRules: CSSRuleList; deleteRule(index: number): void }
type Declared = { readonly style: CSSStyleDeclaration }

function keepOwnDeclarations(rule: Declared): void {
  rule.style.cssText = svgOwnDeclarations(rule.style.cssText)
}

function keepOwnRules(parent: RuleParent, nested: boolean): void {
  for (let i = parent.cssRules.length - 1; i >= 0; i--) {
    const rule = parent.cssRules[i]
    const kind = rule.constructor.name
    if (groupRules.has(kind)) {
      keepOwnRules(rule as CSSGroupingRule, nested)
    } else if (kind === 'CSSStyleRule') {
      const style = rule as CSSStyleRule
      const children = style as unknown as Partial<RuleParent>
      if (!ownSelector(style.selectorText, nested)) {
        parent.deleteRule(i)
        continue
      }
      keepOwnDeclarations(style)
      if (children.cssRules && typeof children.deleteRule === 'function') keepOwnRules(children as RuleParent, true)
      // No nested-rule CSSOM to walk: a rule holding nested rules goes.
      else if (style.cssText.split('{').length > 2) parent.deleteRule(i)
    } else if (!leafRules.has(kind)) {
      parent.deleteRule(i)
    } else if (kind === 'CSSKeyframesRule') {
      for (const frame of Array.from((rule as CSSKeyframesRule).cssRules)) keepOwnDeclarations(frame as CSSKeyframeRule)
    } else if ('style' in rule) {
      keepOwnDeclarations(rule as unknown as Declared)
    }
  }
}

export function svgOwnStylesheet(css: string): string {
  if (typeof CSSStyleSheet !== 'function' || typeof CSSStyleSheet.prototype.replaceSync !== 'function') return ''
  try {
    const sheet = new CSSStyleSheet()
    sheet.replaceSync(css)
    keepOwnRules(sheet, false)
    return Array.from(sheet.cssRules, rule => rule.cssText).join('\n')
  } catch {
    return ''
  }
}
