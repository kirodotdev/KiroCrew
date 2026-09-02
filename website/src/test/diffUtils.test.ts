import { describe, it, expect } from 'vitest'
import { isDiffText, basenamePatchHeaders } from '../utils/diffUtils'

describe('basenamePatchHeaders', () => {
  it('shortens git a/ b/ headers to basenames, keeping the side markers', () => {
    const out = basenamePatchHeaders(
      'diff --git a/website/src/components/DiffBlock.tsx b/website/src/components/DiffBlock.tsx\n' +
      '--- a/website/src/components/DiffBlock.tsx\n' +
      '+++ b/website/src/components/DiffBlock.tsx\n' +
      '@@ -1,2 +1,2 @@\n-old\n+new'
    )
    expect(out).toContain('diff --git a/DiffBlock.tsx b/DiffBlock.tsx')
    expect(out).toContain('--- a/DiffBlock.tsx')
    expect(out).toContain('+++ b/DiffBlock.tsx')
    expect(out).not.toContain('website/src/components')
  })

  it('shortens plain headers and leaves /dev/null naming the absent side', () => {
    const out = basenamePatchHeaders('--- /dev/null\n+++ src/utils/new.ts\n@@ -0,0 +1 @@\n+x')
    expect(out).toContain('--- /dev/null')
    expect(out).toContain('+++ new.ts')
  })

  it('keeps a trailing timestamp attached to the shortened path', () => {
    const out = basenamePatchHeaders('--- a/src/x.ts\t2026-08-17 01:00:00\n+++ b/src/x.ts\t2026-08-17 02:00:00')
    expect(out).toContain('--- a/x.ts\t2026-08-17 01:00:00')
  })

  it('leaves hunk bodies alone, including content that looks like a header', () => {
    const body = '@@ -1,3 +1,3 @@\n ---- not a header\n---- removed line\n+++++ added line'
    expect(basenamePatchHeaders(body)).toBe(body)
  })

  it('is a no-op on a patch whose paths are already bare filenames', () => {
    const patch = '--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n-a\n+b'
    expect(basenamePatchHeaders(patch)).toBe(patch)
  })
})

describe('isDiffText', () => {
  it('returns true for text with @@ hunks', () => {
    expect(isDiffText('@@ -1,3 +1,3 @@\n-old\n+new')).toBe(true)
  })

  it('returns true for text with ---/+++ file headers', () => {
    expect(isDiffText('--- a/file.ts\n+++ b/file.ts\n-old\n+new')).toBe(true)
  })

  it('returns false for plain text', () => {
    expect(isDiffText('just some text')).toBe(false)
  })

  it('returns false for JSON', () => {
    expect(isDiffText('{"key": "value"}')).toBe(false)
  })

  it('returns false for markdown lists', () => {
    expect(isDiffText('- item one\n- item two\n+ not a diff')).toBe(false)
  })

  it('returns false for negative numbers', () => {
    expect(isDiffText('-5 degrees')).toBe(false)
  })

  it('does not false-positive on YAML front matter with +++ heading', () => {
    expect(isDiffText('---\ntitle: doc\n+++ heading')).toBe(false)
  })
})
