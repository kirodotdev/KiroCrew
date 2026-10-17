/**
 * The SDK's default `file` row: a file_send delivery draws the same FileCard
 * ChatPage draws, so ChatEmbed (which has no host override) shows the file
 * instead of an empty gap. Content that is not a file record draws nothing.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ChatMessage } from '../types'
import {
  defaultMessageRenderers,
  resolveRenderer,
  type MessageRenderContext,
} from '../app-sdk/messageRenderers'

const fileMsg = (content: string): ChatMessage =>
  ({ role: 'file', content, cls: '' }) as ChatMessage

const ctx: MessageRenderContext = {
  index: 0,
  messages: [],
  running: false,
  key: 'k',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: children => children,
  row: children => <div data-testid="row">{children}</div>,
}

function draw(content: string) {
  const m = fileMsg(content)
  const entry = resolveRenderer(m, defaultMessageRenderers)
  expect(entry?.id).toBe('file')
  return entry!.render(m, { ...ctx, messages: [m] })
}

describe('default file row', () => {
  it('draws a download card linking to the outbox', () => {
    const out = draw(JSON.stringify({ filename: 'report q3.pdf', size: 2048, content_type: 'application/pdf', description: 'Quarterly report' }))
    expect(out).not.toBeNull()
    render(<>{out}</>)
    expect(screen.getByTestId('row')).toBeTruthy()
    expect(screen.getByText('report q3.pdf')).toBeTruthy()
    expect(screen.getByText('Quarterly report')).toBeTruthy()
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('/api/outbox/report%20q3.pdf')
    expect(link.hasAttribute('download')).toBe(true)
  })

  it('draws an inline image for an image delivery', () => {
    render(<>{draw(JSON.stringify({ filename: 'chart.png', content_type: 'image/png' }))}</>)
    expect(screen.getByRole('img').getAttribute('src')).toBe('/api/outbox/chart.png')
  })

  it.each([
    { content_type: 7 },
    { content_type: {} },
    { content_type: null },
    { description: { text: 'report' } },
    { description: 7 },
    { description: null },
    { size: '2048' },
    { size: {} },
    { size: null },
  ])('rejects malformed card fields: %j', fields => {
    expect(draw(JSON.stringify({ filename: 'report.pdf', ...fields }))).toBeNull()
  })

  it('accepts omitted optional fields and empty string metadata', () => {
    expect(draw(JSON.stringify({ filename: 'report.pdf' }))).not.toBeNull()
    expect(draw(JSON.stringify({ filename: 'report.pdf', content_type: '', description: '', size: 0 }))).not.toBeNull()
  })

  it('draws nothing for content that is not a file record', () => {
    expect(draw('not json')).toBeNull()
    expect(draw('null')).toBeNull()
    expect(draw('"chart.png"')).toBeNull()
    expect(draw(JSON.stringify({ size: 3 }))).toBeNull()
    expect(draw(JSON.stringify({ filename: '' }))).toBeNull()
  })
})
