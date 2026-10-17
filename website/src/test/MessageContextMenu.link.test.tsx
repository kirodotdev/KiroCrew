/**
 * The bubble menu replaces the browser's own, so a right-click on a link inside
 * the bubble must still offer the link copies that native menu had.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import MessageContextMenu, { type MessageMenuItem } from '../pages/chat/MessageContextMenu'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))
import { copyToClipboard } from '../utils/clipboard'

const items: MessageMenuItem[] = [
  { id: 'quote', label: 'Quote message', icon: null, onSelect: () => {} },
  { id: 'copy', label: 'Copy text', icon: null, onSelect: () => {} },
]

const bubble = (
  <div data-testid="bubble">
    <p data-testid="prose">Run build <a href="https://example.com/build/42">42</a> passed.</p>
    <a href="https://example.com/icon" data-testid="icon-link" aria-label="icon"><svg /></a>
  </div>
)

const labels = () => screen.getAllByRole('menuitem').map(i => i.textContent)

beforeEach(() => { vi.mocked(copyToClipboard).mockClear().mockResolvedValue(true) })

describe('MessageContextMenu on a link', () => {
  it('adds Copy link address and Copy link text above the host items', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    expect(labels()).toEqual(['Copy link address', 'Copy link text', 'Quote message', 'Copy text'])
  })

  it('copies the resolved href and the visible text', async () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-address'))
    expect(copyToClipboard).toHaveBeenCalledWith('https://example.com/build/42')
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-text'))
    expect(copyToClipboard).toHaveBeenLastCalledWith('42')
  })

  it('omits Copy link text for a link with no text', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByTestId('icon-link').querySelector('svg')!)
    expect(labels()).toEqual(['Copy link address', 'Quote message', 'Copy text'])
  })

  it('shows only the host items off a link', () => {
    render(<MessageContextMenu items={items}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.keyDown(screen.getByTestId('message-context-menu'), { key: 'Escape' })
    fireEvent.contextMenu(screen.getByTestId('prose'))
    expect(labels()).toEqual(['Quote message', 'Copy text'])
  })

  it('reports a refused clipboard write', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    const onCopyFailed = vi.fn()
    render(<MessageContextMenu items={items} onCopyFailed={onCopyFailed}>{bubble}</MessageContextMenu>)
    fireEvent.contextMenu(screen.getByText('42'))
    fireEvent.click(screen.getByTestId('message-context-copy-link-address'))
    await waitFor(() => expect(onCopyFailed).toHaveBeenCalledTimes(1))
  })
})
