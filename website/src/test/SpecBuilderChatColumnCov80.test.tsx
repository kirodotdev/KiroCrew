// ChatColumn binds a spec to the native chat embed. The slot key must come from
// the server's detail payload when present — the name-derived form is only the
// fallback for entries that predate the persisted key.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

const embedProps: Record<string, unknown>[] = []

vi.mock('../app-sdk/ChatEmbed', () => ({
  default: (p: Record<string, unknown>) => {
    embedProps.push(p)
    return <div data-testid="chat-embed" />
  },
}))

import ChatColumn from '../apps/spec-builder/components/ChatColumn'

describe('ChatColumn', () => {
  it('mounts the embed against the server-provided slot key', () => {
    embedProps.length = 0
    const onSend = vi.fn().mockResolvedValue(undefined)
    render(<ChatColumn name="zz-spec" slotKey="slot-9f" onSend={onSend} />)
    expect(screen.getByTestId('chat-embed')).toBeInTheDocument()
    expect(embedProps[0]).toMatchObject({ slotKey: 'slot-9f', frameless: true, startAtBottom: true })
    expect(embedProps[0].onSend).toBe(onSend)
  })

  it('falls back to a name-derived slot key when none is persisted', () => {
    embedProps.length = 0
    render(<ChatColumn name="zz-spec" onSend={vi.fn()} />)
    expect(embedProps[0].slotKey).toBe('spec-builder-zz-spec')
  })

  it('fills its column so the embed can own the height', () => {
    embedProps.length = 0
    const { container } = render(<ChatColumn name="zz-spec" onSend={vi.fn()} />)
    expect((container.firstElementChild as HTMLElement).className).toContain('flex-1')
  })
})
