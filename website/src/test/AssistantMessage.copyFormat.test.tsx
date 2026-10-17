import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyRichToClipboard: vi.fn().mockResolvedValue(true),
}))
import { copyRichToClipboard, copyToClipboard } from '../utils/clipboard'

const REPLY = '# Plan\n\n- **one**\n- two\n\nSee [docs](https://example.com).'

beforeEach(() => {
  vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
  vi.mocked(copyRichToClipboard).mockReset().mockResolvedValue(true)
})

const openMore = () => fireEvent.pointerDown(
  screen.getByTestId('assistant-more-actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' },
)
// Every surface that adds rich text: Copy in the row (Quote offered), Copy in More (Speak).
const QUOTED = { content: REPLY, isStreaming: false, slotRunning: false, onQuoteMessage: () => {} }
const VOICED = { content: REPLY, isStreaming: false, slotRunning: false, onSpeak: () => {} }

describe('AssistantMessage copy formats', () => {
  it('adds no footer control: the row Copy still copies Markdown on click', async () => {
    render(<AssistantMessage {...QUOTED} />)
    // The tooltip names the format and where the other one is.
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveAttribute('title', 'Copy as Markdown · right-click the reply for rich text')
    const row = screen.getByTestId('assistant-more-actions').parentElement!
    expect(Array.from(row.querySelectorAll(':scope > button')).map(b => b.getAttribute('aria-label'))).toEqual(['Quote message', 'Copy', 'More actions'])
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await act(async () => {})
    expect(copyToClipboard).toHaveBeenCalledWith(REPLY)
    expect(copyRichToClipboard).not.toHaveBeenCalled()
  })

  it('More offers Copy as rich text with a hint naming where it pastes well', () => {
    render(<AssistantMessage {...QUOTED} />)
    openMore()
    const item = screen.getByTestId('copy-rich-menu-item')
    expect(item).toHaveTextContent('Copy as rich text')
    expect(item).toHaveTextContent('Keeps formatting in email and documents')
  })

  it('Copy as rich text writes clean HTML with the Markdown as plain text, and confirms in place', async () => {
    render(<AssistantMessage {...QUOTED} />)
    openMore()
    fireEvent.click(screen.getByTestId('copy-rich-menu-item'))
    await act(async () => {})
    expect(copyRichToClipboard).toHaveBeenCalledTimes(1)
    const [html, plain] = vi.mocked(copyRichToClipboard).mock.calls[0]
    expect(plain).toBe(REPLY)
    expect(html).toContain('<h1>Plan</h1>')
    expect(html).toContain('<strong>one</strong>')
    expect(html).toContain('<a href="https://example.com">docs</a>')
    expect(html).not.toMatch(/class=|style=/)
    expect(copyToClipboard).not.toHaveBeenCalled()
    // The menu stays open so the outcome is visible on the item itself; the
    // hint stays too, so the open menu keeps its size under the pointer.
    expect(screen.getByTestId('copy-rich-menu-item')).toHaveTextContent('Copied as rich text')
    expect(screen.getByTestId('copy-rich-menu-item')).toHaveTextContent('Keeps formatting in email and documents')
    // One confirmation per copy: the row's Markdown Copy does not also claim it.
    expect(screen.getByRole('button', { name: 'Copy', hidden: true })).toBeInTheDocument()
  })

  it('names the Markdown copy "Copy as Markdown" wherever it is a menu item', () => {
    render(<AssistantMessage {...VOICED} />)
    openMore()
    const md = screen.getByTestId('copy-message-menu-item')
    expect(md).toHaveTextContent('Copy as Markdown')
    expect(md).toHaveTextContent('Keeps ** and # symbols, for Markdown editors and chat apps')
    expect(screen.getAllByRole('menuitem').map(i => i.getAttribute('data-testid')).slice(0, 2)).toEqual(['copy-message-menu-item', 'copy-rich-menu-item'])
  })

  it('a refused rich write raises the copy-failed notice', async () => {
    vi.mocked(copyRichToClipboard).mockResolvedValueOnce(false)
    render(<AssistantMessage {...QUOTED} />)
    openMore()
    fireEvent.click(screen.getByTestId('copy-rich-menu-item'))
    await act(async () => {})
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('the right-click menu offers both formats under the same names and hints', async () => {
    render(<AssistantMessage {...QUOTED} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.getByTestId('message-context-copy')).toHaveTextContent('Copy as MarkdownKeeps ** and # symbols, for Markdown editors and chat apps')
    expect(screen.getByTestId('message-context-copy-rich')).toHaveTextContent('Copy as rich textKeeps formatting in email and documents')
    fireEvent.click(screen.getByTestId('message-context-copy-rich'))
    await act(async () => {})
    expect(copyRichToClipboard).toHaveBeenCalledWith(expect.stringContaining('<h1>Plan</h1>'), REPLY)
  })

  it('a rich copy from the right-click menu confirms on the row Copy icon', async () => {
    render(<AssistantMessage {...QUOTED} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    fireEvent.click(screen.getByTestId('message-context-copy-rich'))
    await act(async () => {})
    // The tick names the format it is for, since the icon's own format is Markdown.
    expect(screen.getByRole('button', { name: 'Copied as rich text' })).toHaveAttribute('title', 'Copied as rich text')
  })


  it('a reply with no other menu keeps its row exactly: Copy, Copy link, Pin, raw toggle, and no More', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} />)
    expect(screen.queryByTestId('assistant-more-actions')).not.toBeInTheDocument()
    const row = screen.getByTestId('toggle-raw-view').parentElement!
    expect(Array.from(row.querySelectorAll(':scope > button')).map(b => b.getAttribute('title'))).toEqual(['Copy as Markdown · right-click the reply for rich text', 'Copy link to message', 'Pin message', 'Show raw markdown'])
  })

  it('a reply with no other menu reaches rich copy from the right-click menu', async () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} messageTs="t1" slotKey="chat-1" onTogglePin={() => {}} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.getAllByRole('menuitem').map(i => i.getAttribute('data-testid'))).toEqual(['message-context-copy', 'message-context-copy-rich'])
    fireEvent.click(screen.getByTestId('message-context-copy-rich'))
    await act(async () => {})
    const [html, plain] = vi.mocked(copyRichToClipboard).mock.calls[0]
    expect(plain).toBe(REPLY)
    expect(html).toContain('<h1>Plan</h1>')
    expect(html).not.toMatch(/class=|style=/)
    expect(screen.getByRole('button', { name: 'Copied as rich text' })).toBeInTheDocument()
  })

  it('a right-click on a text selection in a reply without Quote gets the browser menu, so Copy acts on the selection', () => {
    render(<AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} />)
    const bubble = screen.getByTestId('message-bubble')
    const range = document.createRange()
    range.selectNodeContents(screen.getByTestId('md'))
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    try {
      fireEvent.pointerDown(bubble, { button: 2, pointerType: 'mouse' })
      fireEvent.contextMenu(bubble)
      expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
    } finally {
      window.getSelection()!.removeAllRanges()
    }
    // With the selection gone, the same gesture opens the copy menu again.
    fireEvent.pointerDown(bubble, { button: 2, pointerType: 'mouse' })
    fireEvent.contextMenu(bubble)
    expect(screen.getByTestId('message-context-menu')).toBeInTheDocument()
  })

  it('a right-click rich copy on a reply whose Copy sits in More does not claim the Markdown item', async () => {
    render(<AssistantMessage {...VOICED} />)
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    fireEvent.click(screen.getByTestId('message-context-copy-rich'))
    await act(async () => {})
    expect(copyRichToClipboard).toHaveBeenCalledTimes(1)
    openMore()
    expect(screen.getByTestId('copy-message-menu-item')).toHaveTextContent('Copy as Markdown')
    expect(screen.getByTestId('copy-message-menu-item')).not.toHaveTextContent('Copied')
  })

  it('the two copy items carry different glyphs', () => {
    render(<AssistantMessage {...VOICED} />)
    openMore()
    const glyph = (id: string) => screen.getByTestId(id).querySelector('svg')!.getAttribute('class')
    expect(glyph('copy-message-menu-item')).not.toBe(glyph('copy-rich-menu-item'))
  })

  it('a blank reply arms no menu just for copying nothing, and its tooltip promises none', () => {
    render(<AssistantMessage content={' \n '} isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTestId('assistant-more-actions')).not.toBeInTheDocument()
    fireEvent.contextMenu(screen.getByTestId('message-bubble'))
    expect(screen.queryByTestId('message-context-menu')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveAttribute('title', 'Copy as Markdown')
  })

  it('on a touch device, where the bubble menu is not drawn, the tooltip does not point at it', () => {
    const spy = vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
      matches: query === '(pointer: coarse)' || query === '(hover: none)',
      media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }) as MediaQueryList)
    try {
      render(<AssistantMessage {...QUOTED} />)
      expect(screen.getByRole('button', { name: 'Copy' })).toHaveAttribute('title', 'Copy as Markdown')
    } finally {
      spy.mockRestore()
    }
  })
})
