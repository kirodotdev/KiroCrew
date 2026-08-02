import { describe, it, expect, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { detectFileType, ExcalidrawViewer } from '../components/FileRenderers'
import { __resetRendererCache } from '../components/ExcalidrawBlock'

const SCENE = JSON.stringify({
  type: 'excalidraw',
  version: 2,
  elements: [{
    type: 'ellipse', x: 5, y: 5, width: 80, height: 80,
    strokeColor: '#1971c2', backgroundColor: '#a5d8ff', fillStyle: 'solid',
    strokeWidth: 2, strokeStyle: 'solid', roughness: 1, opacity: 100, seed: 11,
  }],
})

describe('detectFileType for Excalidraw scenes', () => {
  it('routes .excalidraw to the diagram viewer', () => {
    // Regression: the extension used to fall through to `code`, so opening a
    // diagram showed a wall of raw element JSON in Monaco.
    expect(detectFileType('/tmp/architecture.excalidraw')).toBe('excalidraw')
    expect(detectFileType('/tmp/UPPER.EXCALIDRAW')).toBe('excalidraw')
  })

  it('does not disturb neighbouring types', () => {
    expect(detectFileType('/tmp/a.json')).toBe('json')
    expect(detectFileType('/tmp/a.excalidraw.svg')).toBe('image')
    expect(detectFileType('/tmp/a.md')).toBe('markdown')
    expect(detectFileType('/tmp/a.py')).toBe('code')
  })
})

describe('ExcalidrawViewer', () => {
  beforeEach(() => { __resetRendererCache() })

  it('draws the scene', async () => {
    const { container } = render(<ExcalidrawViewer content={SCENE} />)
    await waitFor(() => expect(container.querySelector('svg[data-excalidraw-scene]')).not.toBeNull())
    expect(container.querySelectorAll('path').length).toBeGreaterThan(0)
  })

  it('shows the raw source rather than an empty panel when the file is broken', async () => {
    const { container } = render(<ExcalidrawViewer content={'not json at all'} />)
    await waitFor(() =>
      expect(container.querySelector('pre')?.textContent).toBe('not json at all'),
    )
  })

  it('shows the raw source for a valid but empty scene', async () => {
    // An empty canvas has nothing to draw; the user should still see the file
    // contents instead of a blank panel with no explanation.
    const { container } = render(<ExcalidrawViewer content={'{"elements":[]}'} />)
    await waitFor(() =>
      expect(container.querySelector('pre')?.textContent).toContain('elements'),
    )
  })
})
