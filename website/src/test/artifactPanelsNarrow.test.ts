import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const read = (p: string) => readFile(join(__dirname, '..', p), 'utf8')

// Both side panels on the artifact page are fixed-width `shrink-0` siblings of the
// artifact body in a `flex gap-4` row, and the page consulted no viewport hook at
// all. At 390px the 480px chat panel could not fit and was CLIPPED (the row has no
// horizontal scroll, so the overhang was unreachable rather than off-screen), and
// the 340px comments panel left the body 34px.
describe('artifact side panels at phone widths', () => {
  it('gives the companion chat panel the width instead of clipping it', async () => {
    const s = await read('components/ArtifactChatPanel.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s).toMatch(/\$\{isMobile \? 'w-full' : 'w-\[480px\] shrink-0'\}/)
  })

  it('gives the comments sidebar the width too, without breaking caller overrides', async () => {
    const s = await read('components/CommentsSidebar.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s).toMatch(/const SIDEBAR_NARROW_CLASS = 'w-full flex flex-col/)
    // A caller-supplied class must still win, or embedders lose their layout.
    expect(s).toMatch(/containerClassName \?\? \(isMobile \? SIDEBAR_NARROW_CLASS : SIDEBAR_DEFAULT_CLASS\)/)
  })

  it('steps the artifact body aside so an open panel is not splitting 390px', async () => {
    const s = await read('pages/ArtifactDetailPage.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s).toMatch(/\$\{isMobile && panel !== 'none' \? 'hidden' : ''\}/)
    // Hidden, not unmounted: the body holds scroll position and, for markdown, an
    // in-progress anchored comment selection.
    expect(s, 'the body must not be unmounted by a responsive branch')
      .not.toMatch(/\{!\(isMobile && panel !== 'none'\) && \(/)
  })
})
