// Build-only: drop comments from the shipped dashboard shell (dist/index.html).
//
// The source index.html carries long notes for maintainers, both as HTML comments
// and as JS comments inside its inline scripts. The gateway serves the shell with
// `no-store`, so every page load downloads those notes again. This strips them at
// build time and leaves the source untouched.
//
// Inline scripts are parsed with Vite's own JS parser (`parseSync`, oxc) and only
// the comment ranges it reports are cut, so a `//` inside a string, a regex or a
// URL is never mistaken for a comment. Scripts that are not JS (the import map,
// JSON data) are left as they are.
import { parseSync } from 'vite'

const SCRIPT = /(<script\b[^>]*>)([\s\S]*?)(<\/script>)/gi
const HTML_COMMENT = /<!--[\s\S]*?-->/g
const JS_TYPES = new Set(['', 'text/javascript', 'application/javascript', 'module'])

function scriptType(openTag) {
  const m = openTag.match(/\btype\s*=\s*["']?([^"'\s>]+)/i)
  return m ? m[1].toLowerCase() : ''
}

/**
 * Cut [start, end) ranges out of `text`. A range that is alone on its line takes
 * the whole line with it; one that sits between code becomes a single space (or a
 * newline when it spanned lines), so two tokens never fuse and ASI is unchanged.
 */
function cutRanges(text, ranges) {
  let out = ''
  let pos = 0
  for (const [start, end] of ranges) {
    let s = start
    let e = end
    while (s > pos && (text[s - 1] === ' ' || text[s - 1] === '\t')) s -= 1
    let after = e
    while (after < text.length && (text[after] === ' ' || text[after] === '\t')) after += 1
    const atLineStart = s === 0 || text[s - 1] === '\n'
    const atLineEnd = after >= text.length || text[after] === '\n' || text[after] === '\r'
    let filler = ''
    if (atLineStart && atLineEnd) {
      e = after
      if (text[e] === '\r') e += 1
      if (text[e] === '\n') e += 1
    } else if (atLineEnd) {
      e = after
    } else {
      filler = text.slice(start, end).includes('\n') ? '\n' : ' '
      if (atLineStart) s = start
    }
    out += text.slice(pos, s) + filler
    pos = e
  }
  return out + text.slice(pos)
}

function stripJs(code, sourceType) {
  const result = parseSync('inline.js', code, { sourceType, lang: 'js' })
  if (result.errors.length > 0) {
    throw new Error(`strip-html-comments: inline script does not parse: ${result.errors[0].message}`)
  }
  const ranges = result.comments.map((c) => [c.start, c.end])
  for (const [start, end] of ranges) {
    const text = code.slice(start, end)
    // The parser's offsets must land exactly on the comment, or the cut would eat code.
    if (!(text.startsWith('//') || (text.startsWith('/*') && text.endsWith('*/')))) {
      throw new Error(`strip-html-comments: comment offsets do not match the source: ${JSON.stringify(text.slice(0, 40))}`)
    }
  }
  return cutRanges(code, ranges)
}

function stripMarkup(markup) {
  const ranges = [...markup.matchAll(HTML_COMMENT)].map((m) => [m.index, m.index + m[0].length])
  return cutRanges(markup, ranges)
}

/** Remove HTML comments outside scripts and JS comments inside JS scripts. */
export function stripHtmlComments(html) {
  let out = ''
  let pos = 0
  for (const m of html.matchAll(SCRIPT)) {
    const [whole, open, body, close] = m
    out += stripMarkup(html.slice(pos, m.index))
    const type = scriptType(open)
    const isJs = JS_TYPES.has(type) && body.trim() !== ''
    out += open + (isJs ? stripJs(body, type === 'module' ? 'module' : 'script') : body) + close
    pos = m.index + whole.length
  }
  return out + stripMarkup(html.slice(pos))
}

/** Vite plugin: applies `stripHtmlComments` to the built dashboard shell only. */
export function stripHtmlCommentsPlugin() {
  return {
    name: 'kirocrew-strip-html-comments',
    apply: 'build',
    enforce: 'post',
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        return ctx.path === '/index.html' ? stripHtmlComments(html) : html
      },
    },
  }
}
