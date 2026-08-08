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

  it('uses no regex lookahead either, for the same reason', () => {
    // Lookahead is old and safe in every target, but a NEGATIVE lookahead inside a
    // character-class-heavy split is the usual way this creeps back; assert the
    // sentence splitter stayed a plain match.
    expect(src).toContain('s.match(/[^.!?]+(?:[.!?]+|$)/g)')
  })
})
