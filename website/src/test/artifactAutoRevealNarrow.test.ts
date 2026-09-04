import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const read = (p: string) => readFile(join(__dirname, '..', p), 'utf8')

describe('artifact comment auto-reveal at phone widths', () => {
  it('does not auto-open a panel that takes the whole pane', async () => {
    const s = await read('pages/ArtifactDetailPage.tsx')
    // The page opens comments whenever an artifact has any -- written for the
    // side-by-side layout. With the body now stepping aside while narrow, that
    // became a full-pane takeover on first render of any commented artifact.
    expect(s).toMatch(/return commentCount > 0 && !isMobile \? 'comments' : 'none'/)
    expect(s, 'the effect must react to the viewport it now reads')
      .toMatch(/\}, \[slug, commentCount, isMobile\]\)/)
  })

  it('leaves an explicit open alone', async () => {
    const s = await read('pages/ArtifactDetailPage.tsx')
    // The gate must not defeat a manual open: the effect returns before touching
    // `panel` when the user has toggled it.
    expect(s).toMatch(/if \(sidebarUserToggledRef\.current\) return/)
    expect(s).toMatch(/sidebarUserToggledRef\.current = true\n\s*setPanel\(p => \(p === 'comments' \? 'none' : 'comments'\)\)/)
  })

  it('does not close the panel the user just posted into', async () => {
    const s = await read('pages/ArtifactDetailPage.tsx')
    // Clearing the override hands control back to the gated auto-reveal, which
    // answers 'none' while narrow -- closing the panel on the very next
    // `commentCount` change. Revealing after an add IS a user-initiated open.
    expect(s).toMatch(/sidebarUserToggledRef\.current = isMobile/)
    expect(s, 'the callback must depend on the viewport it now reads')
      .toMatch(/\}, \[popover, postCommentMut, isMobile\]\)/)
  })

})
