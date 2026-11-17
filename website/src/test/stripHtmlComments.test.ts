import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

import { stripHtmlComments } from '../../scripts/strip-html-comments.mjs'

describe('stripHtmlComments', () => {
  it('drops HTML comments and lines left empty by them', () => {
    const html = '<head>\n  <!-- a note\n       on two lines -->\n  <meta charset="utf-8" />\n</head>'
    expect(stripHtmlComments(html)).toBe('<head>\n  <meta charset="utf-8" />\n</head>')
  })

  it('drops JS comments in an inline script but keeps // inside strings, regexes and URLs', () => {
    const html = [
      '<script>',
      '    // a whole-line note',
      "    var url = 'https://example.com/a//b'; // trailing note",
      '    var re = /\\/\\//g;',
      '    var x = 1/* inline */+2;',
      '    /* block',
      '       note */',
      '    var s = "<!-- not a comment -->";',
      '</script>',
    ].join('\n')
    expect(stripHtmlComments(html)).toBe(
      [
        '<script>',
        "    var url = 'https://example.com/a//b';",
        '    var re = /\\/\\//g;',
        '    var x = 1 +2;',
        '    var s = "<!-- not a comment -->";',
        '</script>',
      ].join('\n')
    )
  })

  it('leaves the import map and an external module script untouched', () => {
    const map = '<script type="importmap">{"imports":{"a":"/vendor/a.mjs//x"}}</script>'
    const entry = '<script type="module" crossorigin src="/assets/main-AAAAAAAA.js"></script>'
    expect(stripHtmlComments(`<head>\n  ${map}\n  ${entry}\n</head>`)).toBe(`<head>\n  ${map}\n  ${entry}\n</head>`)
  })

  it('refuses an inline script that does not parse rather than cutting it blindly', () => {
    expect(() => stripHtmlComments('<script>var = ;</script>')).toThrow(/does not parse/)
  })

  it('finds a script whose end tag carries a space, so its comments never reach the markup pass', () => {
    const html = '<script>\n  var s = "<!-- kept -->"; // note\n</script >\n<!-- gone -->\n<p></p>'
    expect(stripHtmlComments(html)).toBe('<script>\n  var s = "<!-- kept -->";\n</script >\n<p></p>')
  })

  it('strips every comment from the real index.html and keeps its scripts parseable', () => {
    const src = readFileSync(path.resolve(__dirname, '../../index.html'), 'utf8')
    const out = stripHtmlComments(src)
    expect(out).not.toMatch(/<!--/)
    expect(out.length).toBeLessThan(src.length)
    // Running the strip again finds nothing more, so the output parsed cleanly.
    expect(stripHtmlComments(out)).toBe(out)
    // Every script survives: only comment text was removed.
    const count = (s: string, re: RegExp) => s.match(re)?.length ?? 0
    expect(count(out, /<script\b/g)).toBe(count(src, /<script\b/g))
    expect(count(out, /<\/script>/g)).toBe(count(src, /<\/script>/g))
  })
})
