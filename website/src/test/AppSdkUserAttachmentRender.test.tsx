import { render, screen, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../types'

/* The app-sdk registry's default `user` entry draws every surface that is not
 * ChatPage: ChatPane (split panes, Crew Members DMs), ChatEmbed, SideChat. It
 * used to carry its OWN content renderer that knew paste chips only, so an
 * attached image -- present on the row as `![image](dest)` markdown, or, on
 * pane rows written before the pane serialized attachments, only on
 * `meta.files` -- rendered as nothing there while the same row drew fine on
 * ChatPage. The entry now calls the shared renderUserContent; these pins hold
 * the two surfaces to one rendering. MarkdownRenderer is mocked to expose the
 * markdown it received, which is where an image lives on a user row. */

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string }) => <div data-testid="assistant-message">{props.content}</div>,
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content, compactImages }: { content: string; compactImages?: boolean }) => (
    <span data-testid="markdown" data-compact-images={String(!!compactImages)}>{content}</span>
  ),
}))

import ChatMessageList from '../app-sdk/ChatMessageList'

function user(content: string, meta?: Record<string, unknown>): ChatMessage {
  return { role: 'user', content, cls: '', ts: '2026-09-08T08:00:00Z', ...(meta ? { meta } : {}) }
}

describe('app-sdk user row — attachments render like ChatPage', () => {
  it('renders the image markdown of a row the pane sent (producer form), compact like the main chat', () => {
    render(<ChatMessageList messages={[user('![image](/tmp/shot.png)\n\nwhat is wrong here?')]} running={false} />)
    const md = screen.getByTestId('markdown')
    expect(md.textContent).toContain('![image](/tmp/shot.png)')
    // Sent-prompt images render small on ChatPage (compactImages); the pane
    // must hand MarkdownRenderer the same flag or the two surfaces differ.
    expect(md.getAttribute('data-compact-images')).toBe('true')
  })

  it('heals a legacy pane row whose image lives only on meta.files', () => {
    // Before the pane adopted prepareSendPayload it wrote this shape: typed
    // text verbatim, every attachment on meta.files. resolveFileSegment drops
    // image tokens on the promise that images arrive as markdown, so without
    // the heal this row draws the caption and NO picture.
    render(<ChatMessageList messages={[user('look at this', { files: ['/tmp/legacy.png'] })]} running={false} />)
    const md = screen.getByTestId('markdown')
    expect(md.textContent).toBe('![image](/tmp/legacy.png)\n\nlook at this')
  })

  it('does not double an image the content already names', () => {
    render(<ChatMessageList messages={[user('![image](/tmp/once.png)\n\nhi', { files: ['/tmp/once.png'] })]} running={false} />)
    const text = screen.getByTestId('markdown').textContent || ''
    expect(text.split('![image](/tmp/once.png)').length - 1).toBe(1)
  })

  it('draws a non-image attachment as a card; inert without a file-open handler', () => {
    render(<ChatMessageList messages={[user('summarize this', { files: ['/tmp/report.pdf'] })]} running={false} />)
    const card = screen.getByTitle(/\/tmp\/report\.pdf/)
    expect(card.textContent).toContain('report.pdf')
    // No host handler: the card must not LOOK clickable (no button role, no
    // open-file label), the same degrade DirChip makes -- and its tooltip says
    // WHERE the file opens, since a click here answers nothing.
    expect(card.getAttribute('role')).toBeNull()
    expect(card.getAttribute('title')).toMatch(/can't be opened here/)
    expect(screen.queryByRole('button', { name: /report\.pdf/ })).toBeNull()
  })

  it('opens the attachment through the host handler when one is supplied', () => {
    const onFileOpen = vi.fn()
    render(<ChatMessageList messages={[user('summarize this', { files: ['/tmp/report.pdf'] })]} running={false} onFileOpen={onFileOpen} />)
    fireEvent.click(screen.getByTitle('/tmp/report.pdf'))
    expect(onFileOpen).toHaveBeenCalledWith('/tmp/report.pdf')
  })
})
