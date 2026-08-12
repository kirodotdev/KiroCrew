import { describe, it, expect } from 'vitest'
import { parseDirTokens, dirFullPath, serializeDirTokens, parseDirs, resolveDirSegment, spliceDirTokens } from '../utils/fileTokens'

describe('parseDirTokens', () => {
  it('extracts boundary-checked @rel/ tokens in appearance order', () => {
    const t = parseDirTokens('check @docs/ and @src/pages/ please')
    expect(t.map(x => x.rel)).toEqual(['docs/', 'src/pages/'])
    expect(t[0].token).toBe('@docs/')
  })

  it('dedupes a repeated token', () => {
    expect(parseDirTokens('@docs/ then @docs/ again')).toHaveLength(1)
  })

  it('ignores file tokens (no trailing slash) and mid-word @', () => {
    expect(parseDirTokens('see @src/main.ts and mail a@b.c/ now')).toHaveLength(0)
  })

  it('ignores URLs and slash-only bodies', () => {
    expect(parseDirTokens('go to @https://example.com/ or @/ maybe')).toHaveLength(0)
  })
})

describe('dirFullPath', () => {
  it('joins a relative token against the project root, without trailing slash', () => {
    expect(dirFullPath('src/pages/', '/repo')).toBe('/repo/src/pages')
  })

  it('passes an absolute rel through unchanged (picker fallback outside the root)', () => {
    expect(dirFullPath('/other/place/', '/repo')).toBe('/other/place')
  })

  it('joins with a backslash for a Windows project root', () => {
    expect(dirFullPath('src\\pages\\', 'C:\\repo')).toBe('C:\\repo\\src\\pages')
  })
})

describe('serializeDirTokens', () => {
  it('rewrites each token to [attached_dir N] with the absolute path', () => {
    const { llm, dirPaths } = serializeDirTokens('look in @docs/ and @src/pages/ now', '/repo')
    expect(llm).toBe('look in [attached_dir 1] /repo/docs and [attached_dir 2] /repo/src/pages now')
    expect(dirPaths).toEqual(['/repo/docs', '/repo/src/pages'])
  })

  it('is a no-op without folder tokens', () => {
    const { llm, dirPaths } = serializeDirTokens('plain text with @file.ts', '/repo')
    expect(llm).toBe('plain text with @file.ts')
    expect(dirPaths).toEqual([])
  })

  it('a repeated token gets the same marker number', () => {
    const { llm, dirPaths } = serializeDirTokens('@docs/ and @docs/ again', '/repo')
    expect(llm).toBe('[attached_dir 1] /repo/docs and [attached_dir 1] /repo/docs again')
    expect(dirPaths).toEqual(['/repo/docs'])
  })

  it('never rewrites inside a longer sibling token', () => {
    const { llm } = serializeDirTokens('@src/pages/ vs @src/pages/sub/', '/repo')
    expect(llm).toBe('[attached_dir 1] /repo/src/pages vs [attached_dir 2] /repo/src/pages/sub')
  })

  it('inserts a path containing replacement patterns literally', () => {
    // `$1` / `$&` in a folder name must survive: a template-string replacement
    // would expand them as regex replacement patterns and corrupt the marker.
    const { llm, dirPaths } = serializeDirTokens('see @$1/ now', '/re$&po')
    expect(dirPaths).toEqual(['/re$&po/$1'])
    expect(llm).toBe('see [attached_dir 1] /re$&po/$1 now')
  })
})

describe('parseDirs', () => {
  it('prefers meta.dirs over content markers', () => {
    expect(parseDirs('[attached_dir 1] /wrong', { dirs: ['/repo/docs'] })).toEqual(['/repo/docs'])
  })

  it('falls back to scanning markers when meta is absent', () => {
    expect(parseDirs('see [attached_dir 1] /repo/docs and [attached_dir 2] /repo/src')).toEqual(['/repo/docs', '/repo/src'])
  })
})

describe('resolveDirSegment', () => {
  it('rewrites markers back to @label/ tokens with a label->path map', () => {
    const { display, dirMentionMap } = resolveDirSegment('see [attached_dir 1] /repo/docs now', ['/repo/docs'])
    expect(display).toBe('see @docs/ now')
    expect(dirMentionMap.get('docs/')).toBe('/repo/docs')
  })

  it('recovers a path with spaces losslessly via the meta index', () => {
    const p = '/repo/my docs'
    const { display, dirMentionMap } = resolveDirSegment(`see [attached_dir 1] ${p} now`, [p])
    expect(display).toBe('see @my docs/ now')
    expect(dirMentionMap.get('my docs/')).toBe(p)
  })

  it('widens colliding basenames so both chips resolve distinctly', () => {
    const { display, dirMentionMap } = resolveDirSegment(
      '[attached_dir 1] /a/pages and [attached_dir 2] /b/pages',
      ['/a/pages', '/b/pages'],
    )
    expect(display).toBe('@a/pages/ and @b/pages/')
    expect(dirMentionMap.get('a/pages/')).toBe('/a/pages')
    expect(dirMentionMap.get('b/pages/')).toBe('/b/pages')
  })

  it('labels a Windows path by segment, not as one giant string', () => {
    const p = 'C:\\repo\\src\\widgets'
    const { display, dirMentionMap } = resolveDirSegment(`see [attached_dir 1] ${p} now`, [p])
    expect(display).toBe('see @widgets/ now')
    expect(dirMentionMap.get('widgets/')).toBe(p)
  })

  it('maps fresh @rel/ display tokens to their meta path (optimistic bubble shape)', () => {
    const { display, dirMentionMap } = resolveDirSegment('look in @src/pages/ now', ['/repo/src/pages'])
    expect(display).toBe('look in @src/pages/ now')
    expect(dirMentionMap.get('src/pages/')).toBe('/repo/src/pages')
  })

  it('leaves unrelated text untouched', () => {
    const { display, dirMentionMap } = resolveDirSegment('no folders here', [])
    expect(display).toBe('no folders here')
    expect(dirMentionMap.size).toBe(0)
  })
})

describe('spliceDirTokens', () => {
  it('appends a boundary-checked @path/ token when the caret is unknown', () => {
    const out = spliceDirTokens('', null, ['/Users/me/demo'])
    expect(out.value).toBe('@/Users/me/demo/ ')
    expect(out.caret).toBe(out.value.length)
    // The inserted token round-trips through the chip parser.
    expect(parseDirTokens(out.value).map(t => t.rel)).toEqual(['/Users/me/demo/'])
  })

  it('pads with a leading space when appending after existing text', () => {
    const out = spliceDirTokens('look here', null, ['docs'])
    expect(out.value).toBe('look here @docs/ ')
  })

  it('inserts at the caret with whitespace padding on both sides', () => {
    //            0123456789
    const out = spliceDirTokens('check then send', 5, ['src/pages'])
    expect(out.value).toBe('check @src/pages/  then send')
    expect(out.caret).toBe('check @src/pages/ '.length)
    expect(parseDirTokens(out.value).map(t => t.rel)).toEqual(['src/pages/'])
  })

  it('keeps an existing trailing separator instead of doubling it', () => {
    expect(spliceDirTokens('', null, ['docs/']).value).toBe('@docs/ ')
    // Windows rel ending in a backslash is left as-is (selectionFor parity).
    expect(spliceDirTokens('', null, ['src\\pages\\']).value).toBe('@src\\pages\\ ')
  })

  it('skips a rel whose token is already present in the text', () => {
    const out = spliceDirTokens('see @docs/ now', null, ['docs'])
    expect(out.value).toBe('see @docs/ now')
    // changed=false lets callers skip the state write and caret arm entirely —
    // arming a caret restore against an unchanged value would fire stale later.
    expect(out.changed).toBe(false)
  })

  it('reports changed=true when at least one token was inserted', () => {
    expect(spliceDirTokens('see @docs/ now', null, ['docs', 'src']).changed).toBe(true)
  })

  it('dedupes within one batch', () => {
    const out = spliceDirTokens('', null, ['docs', 'docs/'])
    expect(out.value).toBe('@docs/ ')
  })

  it('inserts several folders as separate tokens', () => {
    const out = spliceDirTokens('', null, ['a', 'b'])
    expect(parseDirTokens(out.value).map(t => t.rel)).toEqual(['a/', 'b/'])
  })

  it('clamps an out-of-range caret to the value length', () => {
    const out = spliceDirTokens('hi', 99, ['docs'])
    expect(out.value).toBe('hi @docs/ ')
  })
})
