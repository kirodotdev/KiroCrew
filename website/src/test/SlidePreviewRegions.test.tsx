// @vitest-environment jsdom
//
// jsdom for the same reason as SlidePreviewSanitize.test.tsx: every fragment goes
// through DOMPurify, which mishandles happy-dom's parser.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ComposePayload } from '../apps/pptx-maker/api'

const payload: { current: ComposePayload } = {
  current: { version: 1, viewBox: '0 0 1920 1080', components: [] },
}

vi.mock('../apps/pptx-maker/api', async () => {
  const actual = await vi.importActual<typeof import('../apps/pptx-maker/api')>('../apps/pptx-maker/api')
  return { ...actual, fetchArtifactJson: vi.fn(async () => payload.current) }
})

async function renderPreview() {
  const { default: SlidePreview } = await import('../apps/pptx-maker/SlidePreview')
  return render(<SlidePreview composeUrl="preview/d/compose/s_1.json" defs={null} label="s" />)
}

describe('SlidePreview layout regions', () => {
  beforeEach(() => {
    payload.current = { version: 1, viewBox: '0 0 1920 1080', components: [] }
  })

  it('draws an unfilled region with its name as a text node', async () => {
    payload.current = {
      ...payload.current,
      regions: [{ name: '<img src=x onerror=alert(1)>', x: 96, y: 280, w: 552, h: 640 }],
    }
    const { container } = await renderPreview()
    await waitFor(() => expect(container.querySelector('g[data-region]')).not.toBeNull())
    const label = container.querySelector('g[data-region] text')
    expect(label?.textContent).toBe('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
  })

  it('does not draw a region the content has already filled', async () => {
    payload.current = {
      ...payload.current,
      components: [{ svg: '<text>30%</text>', text: '30%', bbox: { x: 120, y: 300, w: 400, h: 200 } }],
      regions: [{ name: 'metric-1', x: 96, y: 280, w: 552, h: 640 }],
    }
    const { container } = await renderPreview()
    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull())
    expect(container.querySelector('g[data-region]')).toBeNull()
  })

  it('sizes the frame from the compose viewBox for a non-16:9 template', async () => {
    payload.current = { version: 1, viewBox: '0 0 1440 1080', components: [] }
    const { container } = await renderPreview()
    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull())
    const frame = container.firstElementChild as HTMLElement
    await waitFor(() => expect(frame.style.aspectRatio).toBe('1440 / 1080'))
  })
})
