import { describe, expect, it } from 'vitest'

import { patchChangeForStatus, withUnifiedPatchHeaders } from '../components/unifiedPatchHeaders'

const PATCH = '@@ -1 +1 @@\n-old\n+new'

describe('withUnifiedPatchHeaders', () => {
  it('leaves ordinary paths bare and byte-stable', () => {
    const text = withUnifiedPatchHeaders('src/app.ts', PATCH)
    expect(text).toBe(`diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n${PATCH}`)
  })

  it('spells the mode line that carries the change type', () => {
    expect(withUnifiedPatchHeaders('f', PATCH, 'new')).toContain('new file mode 100644')
    expect(withUnifiedPatchHeaders('f', PATCH, 'new')).toContain('--- /dev/null')
    expect(withUnifiedPatchHeaders('f', PATCH, 'deleted')).toContain('deleted file mode 100644')
    expect(withUnifiedPatchHeaders('f', PATCH, 'deleted')).toContain('+++ /dev/null')
  })

  it('C-quotes a newline-bearing path so it cannot inject header lines', () => {
    const text = withUnifiedPatchHeaders('evil\nname.ts', PATCH)
    const headerLines = text.split('\n').slice(0, 3)
    // The raw newline must not survive into the line-oriented headers: every
    // header stays one line, with the path quoted the way git itself writes it.
    expect(headerLines[0]).toBe('diff --git "a/evil\\nname.ts" "b/evil\\nname.ts"')
    expect(headerLines[1]).toBe('--- "a/evil\\nname.ts"')
    expect(headerLines[2]).toBe('+++ "b/evil\\nname.ts"')
    expect(text.endsWith(PATCH)).toBe(true)
  })

  it('escapes quotes and backslashes inside a quoted name', () => {
    const text = withUnifiedPatchHeaders('a"b\\c\td.ts', PATCH)
    expect(text.split('\n')[0]).toBe('diff --git "a/a\\"b\\\\c\\td.ts" "b/a\\"b\\\\c\\td.ts"')
  })
})

describe('patchChangeForStatus', () => {
  it('maps provider tokens onto the two icon-changing types', () => {
    expect(patchChangeForStatus('added')).toBe('new')
    expect(patchChangeForStatus('removed')).toBe('deleted')
    expect(patchChangeForStatus('renamed')).toBe('change')
    expect(patchChangeForStatus('modified')).toBe('change')
  })
})
