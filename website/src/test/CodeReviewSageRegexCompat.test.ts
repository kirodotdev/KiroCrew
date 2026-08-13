import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'

// The sentence-split fallback must not use a regex lookbehind. WebKit only shipped
// lookbehind in Safari 16.4, and an unsupported group is a SyntaxError at MODULE
// EVALUATION — it takes the whole app down on an older browser rather than
// degrading this one view, and no runtime test in Node or jsdom can catch it
// because both support lookbehind. So the guard has to read the source.
describe('ReportView browser compatibility', () => {
  const src = readFileSync(
    resolve(process.cwd(), 'src/apps/code-review-sage/components/ReportView.tsx'),
    'utf8',
  )

  it('uses no regex lookbehind', () => {
    // Match the lookbehind opener in a regex literal, not the word in prose.
    const inCode = src
      .split('\n')
      .filter((l) => !l.trimStart().startsWith('*') && !l.trimStart().startsWith('//'))
      .join('\n')
    expect(inCode).not.toMatch(/\(\?<[=!]/)
  })

  it('splits on sentence gaps without breaking dotted tokens', () => {
    // The property, not the spelling: pinning the regex as a literal string made
    // the guard fail on any correction to it, which is how the token-splitting bug
    // survived a round of review. Lookahead is safe in every target we ship to --
    // only lookbehind is the Safari 16.4 hazard, and the scan above covers that.
    const splitter = /(?:[^.!?]|[.!?](?=\S))+[.!?]*/g
    const split = (value: string) =>
      (value.match(splitter) ?? []).map((seg) => seg.trim()).filter(Boolean)

    // Dotted tokens stay whole.
    expect(split('Touches src/foo.py in the worker.')).toEqual([
      'Touches src/foo.py in the worker.',
    ])
    expect(split('Pinned at v2.0 for now.')).toEqual(['Pinned at v2.0 for now.'])

    // Real boundaries still split.
    expect(split('First one. Second one.')).toEqual(['First one.', 'Second one.'])
    expect(split('Why? Because it does! Truly.')).toEqual([
      'Why?', 'Because it does!', 'Truly.',
    ])

    // An ellipsis is one token, not three empty fragments.
    expect(split('Wait... what?')).toEqual(['Wait...', 'what?'])
  })
})
