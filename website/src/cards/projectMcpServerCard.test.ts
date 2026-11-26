/**
 * The `mcp.project_server` card: a server a folder's own MCP file names, held back
 * when the chat started. Its buttons say what they do: Allow <server> in this folder, Skip for
 * this chat (the server stays off here; the next chat in this folder asks again).
 */
import { describe, it, expect } from 'vitest'
import { CARD_REGISTRY, cardTitle } from './cardRegistry'
import type { Card } from '../api/cards'

describe('mcp.project_server card', () => {
  it('names its buttons by result and keeps the gateway title', () => {
    const spec = CARD_REGISTRY['mcp.project_server']
    const card = { kind: 'mcp.project_server', params: { server: 'planted' }, title: '', changes: [] } as unknown as Card
    expect(spec.primary(card)).toBe('Allow planted in this folder')
    expect(spec.primary()).toBe('Allow in this folder')
    expect(spec.decline?.()).toBe('Skip for this chat')
    const title = "Start planted from this folder's .mcp.json?"
    expect(cardTitle({ kind: 'mcp.project_server', params: {}, title, changes: [] })).toBe(title)
  })
})
