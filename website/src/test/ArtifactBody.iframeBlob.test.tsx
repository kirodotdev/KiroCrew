import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Artifact } from '../types'

// review finding (post 24, ArtifactBody.tsx:169): when the
// iframe body's `srcdoc` transitions from a truthy value to null (e.g. the
// artifact content empties while the panel is open), the effect's cleanup
// revokes the old blob URL but `blobUrl` state must ALSO be cleared — otherwise
// the iframe keeps pointing at a dead (revoked) blob and shows a broken page.
// These tests pin that `setBlobUrl(null)` clear-on-falsy behavior.

// Constants (no magic literals / no source-constant imports).
const SLUG = 'my-widget'
const HTML_CONTENT = '<p>hello</p>'
const RENDERING_PLACEHOLDER = /Rendering/i
const BLOB_URL = 'blob:mock/widget-url'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

// The comment bridge touches iframe internals we don't exercise here.
vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

// srcdoc is non-null exactly when artifact.content is non-empty — mirror that
// 1:1 so the test drives the truthy -> falsy transition via content alone.
vi.mock('../lib/widgetSrcdoc', () => ({
  THEME_VAR_NAMES: [] as string[],
  buildSrcdoc: ({ html }: { html: string }) => html,
}))

import { ArtifactBodyIframe } from '../components/ArtifactBody'

function makeArtifact(content: string): Artifact {
  // Minimal widget artifact; only `content` and `kind` drive the iframe path.
  return { slug: SLUG, name: 'Widget', kind: 'widget', content } as unknown as Artifact
}

describe('ArtifactBodyIframe blob URL lifecycle', () => {
  let createSpy: ReturnType<typeof vi.fn>
  let revokeSpy: ReturnType<typeof vi.fn>
  // Capture the originals so we can fully restore them — these are direct
  // property assignments on the URL global, which vi.restoreAllMocks() does
  // NOT undo. Leaving the stubs in place would leak into later test files in
  // the same worker (order-dependent failures under --coverage sharding).
  const originalCreate = globalThis.URL.createObjectURL
  const originalRevoke = globalThis.URL.revokeObjectURL

  beforeEach(() => {
    // jsdom does not implement object URLs; stub them.
    createSpy = vi.fn(() => BLOB_URL)
    revokeSpy = vi.fn()
    // @ts-expect-error - assigning test stubs onto the URL global
    globalThis.URL.createObjectURL = createSpy
    // @ts-expect-error - assigning test stubs onto the URL global
    globalThis.URL.revokeObjectURL = revokeSpy
  })

  afterEach(() => {
    // Restore the originals (the global setup's no-op stubs) so this file's
    // spies never leak into later test files in the same worker.
    globalThis.URL.createObjectURL = originalCreate
    globalThis.URL.revokeObjectURL = originalRevoke
    vi.restoreAllMocks()
  })

  it('renders the iframe against a freshly minted blob URL when content is present', () => {
    // given/when an artifact with html content is rendered
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} slug={SLUG} />)
    // then a blob URL is created and the iframe references it
    expect(createSpy).toHaveBeenCalledTimes(1)
    const iframe = document.querySelector('iframe')
    expect(iframe).not.toBeNull()
    expect(iframe?.getAttribute('src')).toBe(BLOB_URL)
  })

  it('clears the stale blob URL when content empties (no dead-blob iframe)', () => {
    // given an iframe rendered against a live blob URL
    const { rerender } = render(
      <ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} slug={SLUG} />,
    )
    expect(document.querySelector('iframe')).not.toBeNull()

    // when the artifact content empties (srcdoc -> null) while the panel is open
    rerender(<ArtifactBodyIframe artifact={makeArtifact('')} slug={SLUG} />)

    // then the old blob URL is revoked AND blobUrl state is cleared, so the
    // iframe is unmounted (falls back to the placeholder) rather than pointing
    // at the now-revoked URL
    expect(revokeSpy).toHaveBeenCalledWith(BLOB_URL)
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByText(RENDERING_PLACEHOLDER)).toBeTruthy()
  })
})
