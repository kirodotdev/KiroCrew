/**
 * The `mcp.server` card: an agent's ask to add, change or remove an MCP server.
 * Its title names the action and the server in the reader's language, its
 * buttons are Apply and Cancel, its reason row reads Why, and its options
 * re-preview the card: Turn on MCP server sharing (with Learn more, offered only
 * for a server that is safe to share) and Restart open chats to use it.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Card, CardChange } from '../api/cards'
import { McpServerOptions } from './kindBodies'
import { CARD_REGISTRY, cardChanges, cardTitle } from './cardRegistry'
import { CardChanges } from './CardChanges'

const offer = (after: boolean): CardChange => ({ field: 'share_offer', label: 'Sharing', after })

const params = { action: 'add', server: 'weather', entry: { command: 'npx' }, use_now: true }

const serverCard = (over: Partial<Card> = {}): Card => ({
  id: 'm1',
  slot_key: 'slot-A',
  kind: 'mcp.server',
  revision: 1,
  status: 'pending',
  risk: 'code_exec',
  title: 'Add server: weather',
  changes: [
    { label: 'Command', after: 'npx -y @acme/weather-mcp' },
    { label: 'Environment', add: ['CITY=Paris'] },
  ],
  scope: null,
  reason: null,
  editable: ['use_now', 'share', 'cwd_use'],
  params,
  plan: { apply: [], undo: null },
  created_at: 1_700_000_000,
  expires_at: 4_000_000_000_000,
  ...over,
} as Card)

const renderOptions = (card: Card, preview = vi.fn()) =>
  render(<MemoryRouter><McpServerOptions card={card} disabled={false} preview={preview} /></MemoryRouter>)

describe('mcp.server card', () => {
  it.each([
    ['add', 'Add server: weather'],
    ['change', 'Change server: weather'],
    ['remove', 'Remove server: weather'],
  ])('titles a %s card by its action and server', (action, title) => {
    expect(cardTitle(serverCard({ params: { action, server: 'weather' } }))).toBe(title)
  })

  it('names its buttons by result and its reason row Why', () => {
    const spec = CARD_REGISTRY['mcp.server']
    expect(spec.primary()).toBe('Apply')
    expect(spec.decline?.()).toBe('Cancel')
    expect(spec.reasonLabel?.()).toBe('Why')
  })

  it('draws the sharing offer as its option, not as a change line', () => {
    const card = serverCard({ changes: [...serverCard().changes, offer(true)] })
    expect(cardChanges(card).map(c => c.label)).toEqual(['Command', 'Environment'])
  })

  it('draws a stop-sharing row as a change line beside the offer', () => {
    const off: CardChange = { label: 'Sharing', before: 'On', after: 'Off' }
    const card = serverCard({ changes: [...serverCard().changes, off, offer(false)] })
    expect(cardChanges(card)).toEqual([...serverCard().changes, off])
    render(<MemoryRouter><CardChanges changes={cardChanges(card)} /></MemoryRouter>)
    expect(screen.getByText('Sharing')).toBeTruthy()
    expect(screen.getByText('On')).toBeTruthy()
    expect(screen.getByText('Off')).toBeTruthy()
  })

  it('has one restart line, checked by default, and re-previews with use_now', () => {
    const preview = vi.fn()
    renderOptions(serverCard(), preview)
    const box = screen.getByTestId('change-card-mcp-use-now-box') as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(screen.getByLabelText('Restart open chats to use it')).toBe(box)
    fireEvent.click(box)
    expect(preview).toHaveBeenLastCalledWith({ ...params, use_now: false })
  })

  it('words the restart line on a remove card by what it stops', () => {
    renderOptions(serverCard({ params: { action: 'remove', server: 'weather', use_now: true } }))
    const box = screen.getByTestId('change-card-mcp-use-now-box')
    expect(screen.getByLabelText('Restart open chats to stop using it')).toBe(box)
  })

  it('offers no sharing when the gateway made no offer', () => {
    renderOptions(serverCard())
    expect(screen.queryByTestId('change-card-mcp-share-box')).toBeNull()
  })

  it('offers sharing with a Learn more link to the Sharing view', () => {
    renderOptions(serverCard({ changes: [offer(false)] }))
    expect(screen.getByLabelText('Turn on MCP server sharing')).toBeTruthy()
    expect(screen.getByTestId('change-card-mcp-share-learn-more').getAttribute('href')).toBe('/mcp?view=sharing')
  })

  it('asks no question: sharing is one box and the restart line', () => {
    renderOptions(serverCard({ changes: [offer(true)] }))
    expect(screen.queryByRole('radiogroup')).toBeNull()
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it('re-previews with share flipped', () => {
    const preview = vi.fn()
    renderOptions(serverCard({ changes: [offer(true)] }), preview)
    fireEvent.click(screen.getByTestId('change-card-mcp-share-box'))
    expect(preview).toHaveBeenLastCalledWith({ ...params, share: false })
  })

  it('cannot be changed once the card is applied', () => {
    renderOptions(serverCard({ status: 'applied', changes: [offer(true)] }))
    expect((screen.getByTestId('change-card-mcp-use-now-box') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByTestId('change-card-mcp-share-box') as HTMLInputElement).disabled).toBe(true)
  })
})
