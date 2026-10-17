import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { createRef } from 'react'

import { ArtifactBodyNative, fileTypeForKind } from '../components/ArtifactBody'
import type { Artifact } from '../types'

/**
 * A `kind="dashboard"` artifact must not reach the prose renderer.
 *
 * Its content is a layout PACKAGE, not a document: `src/kiro_crew/artifact_store/
 * dashboard_package.py` canonicalises every dashboard write into a JSON object
 * holding `bound_to` / `model` / `view` / `theme`, so the stored bytes are
 * always JSON. `fileTypeForKind` had no case for it, and the default branch
 * returns `'markdown'`, so the detail page flowed the package through
 * MarkdownRenderer: newlines collapsed, and markup inside a label was
 * interpreted rather than shown (`<b>u</b>` rendered as a bold "u"). Nothing
 * executed — the markdown pipeline refuses scripts — so this is a readability
 * defect, and these tests assert readability, not containment.
 */

/** A package shaped like a real one, with markup in the labels a reader must
 *  see literally. */
const PACKAGE = JSON.stringify(
  {
    bound_to: 'crewmate:qa',
    model: { fields: [{ name: 'open_items', type: 'count' }] },
    view: { blocks: [{ type: 'stat', label: '<b>u</b>', note: '<script>x</script>' }] },
    theme: { tokens: { accent: 'oklch(0.7 0.1 250)' } },
  },
  null,
  2,
)

function renderBody(kind: Artifact['kind'], content: string) {
  return render(
    <ArtifactBodyNative
      kind={kind}
      content={content}
      editing={false}
      onChange={vi.fn()}
      previewRef={createRef<HTMLDivElement>()}
    />,
  )
}

/** The json view opens the first two levels and leaves the rest behind a
 *  disclosure, so a block label sits one or two clicks in. Click every closed
 *  node until none is left, which is what a reader does to read a package. */
function expandAll(container: HTMLElement) {
  for (let pass = 0; pass < 10; pass++) {
    const closed = [...container.querySelectorAll('button')]
      .filter(b => b.textContent?.startsWith('▶'))
    if (closed.length === 0) return
    closed.forEach(b => fireEvent.click(b))
  }
  throw new Error('the json view still had collapsed nodes after 10 passes')
}

describe('fileTypeForKind — which kinds reach the prose renderer', () => {
  it('maps dashboard to the json view, not markdown', () => {
    expect(fileTypeForKind('dashboard')).toBe('json')
  })

  /** The default branch exists for unexpected enum values and must stay, but
   *  nothing that REACHES ArtifactBodyNative may rely on it: those kinds are
   *  the ones a reader sees as prose. Pinned per kind so adding a kind to the
   *  union without a case here fails this test instead of shipping a package
   *  rendered as a paragraph. */
  it('gives every natively-rendered kind its own case', () => {
    const map: Record<string, string> = {
      markdown: 'markdown',
      text: 'code',
      json: 'json',
      svg: 'svg',
      image: 'image',
      dashboard: 'json',
    }
    for (const [kind, fileType] of Object.entries(map)) {
      expect(fileTypeForKind(kind as Artifact['kind'])).toBe(fileType)
    }
  })

  /** widget and html never reach ArtifactBodyNative — ArtifactDetailPage and
   *  ArtifactPanel both route them to ArtifactBodyIframe — so their fall-through
   *  to markdown is the documented survivable fallback, not a prose render. */
  it('leaves the iframe kinds on the fallback', () => {
    expect(fileTypeForKind('widget')).toBe('markdown')
    expect(fileTypeForKind('html')).toBe('markdown')
  })
})

describe('ArtifactBodyNative — a dashboard package reads as its own text', () => {
  it('shows markup inside a label literally', () => {
    const { container } = renderBody('dashboard', PACKAGE)
    expandAll(container)
    // The json view stringifies each leaf, so the markup is text in the DOM and
    // no element is built from it.
    expect(container.textContent).toContain('<b>u</b>')
    expect(container.textContent).toContain('<script>x</script>')
    expect(container.querySelector('b')).toBeNull()
    // The markdown pipeline's refusal placeholder is what the reader saw
    // instead of the stored bytes.
    expect(container.textContent).not.toContain('[unsupported: script]')
  })

  it('keeps the package structure visible', () => {
    const { container } = renderBody('dashboard', PACKAGE)
    for (const key of ['bound_to', 'model', 'view', 'theme']) {
      expect(container.textContent).toContain(key)
    }
  })

  /** Positive control. The same bytes under `markdown` DO build an element from
   *  the label, which is the behaviour the fix moves dashboard off — without
   *  this, the assertions above could pass on an empty render. */
  it('still interprets the same markup on the markdown path', () => {
    const { container } = renderBody('markdown', PACKAGE)
    expect(container.querySelector('b')).not.toBeNull()
  })
})
