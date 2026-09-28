/**
 * ChatNavContent — the presentational Navigation body of the activity sidebar.
 * Two independent lists, each with a populated and an empty state, plus a
 * type-badge lookup that must fall back for an unknown link type and an
 * outline row that reports the DISPLAY index (not the array index) back to the
 * host so the virtualized transcript scrolls to the right row.
 *
 * Copy is looked up through i18nT rather than hardcoded, so a wording change
 * cannot break these assertions.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ChatNavContent from '../pages/chat/ChatNavPanel'
import { i18nT } from '../i18n/t'
import type { ExtractedLink } from '../utils/extractChatLinks'
import type { ChatSection } from '../hooks/useChatNavigation'

const link = (over: Partial<ExtractedLink> = {}): ExtractedLink => ({
  url: 'https://zzq.example/a',
  type: 'other',
  label: 'zzq-label',
  msgIdx: 0,
  ...over,
})

const section = (over: Partial<ChatSection> = {}): ChatSection => ({
  label: 'zzq-section',
  msgIdx: 0,
  displayIdx: 0,
  ...over,
})

describe('ChatNavContent', () => {
  it('renders both empty states when there are no links and no sections', () => {
    render(<ChatNavContent links={[]} sections={[]} onScrollToSection={() => {}} />)
    expect(screen.getByText(i18nT('pages.chat.chatNavPanel.no_links_found'))).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.chat.chatNavPanel.start_chatting_to_see_sections'))).toBeInTheDocument()
  })

  it('renders a resource row with its type badge and href', () => {
    render(
      <ChatNavContent
        links={[link({ type: 'taskei', label: 'zzq-task', url: 'https://zzq.example/task' })]}
        sections={[]}
        onScrollToSection={() => {}}
      />,
    )
    expect(screen.getByText('Taskei')).toBeInTheDocument()
    const anchor = screen.getByRole('link', { name: /zzq-task/ })
    expect(anchor).toHaveAttribute('href', 'https://zzq.example/task')
    expect(anchor).toHaveAttribute('target', '_blank')
  })

  it('falls back to the generic badge label for an unknown link type', () => {
    render(
      <ChatNavContent
        links={[link({ type: 'zzq-unknown' as ExtractedLink['type'] })]}
        sections={[]}
        onScrollToSection={() => {}}
      />,
    )
    expect(screen.getByText(i18nT('pages.chat.chatNavPanel.link'))).toBeInTheDocument()
  })

  it('drops a duplicate resource so the same target is listed once', () => {
    render(
      <ChatNavContent
        links={[
          link({ url: 'https://zzq.example/dup', label: 'zzq-first' }),
          link({ url: 'https://zzq.example/dup', label: 'zzq-second', msgIdx: 3 }),
        ]}
        sections={[]}
        onScrollToSection={() => {}}
      />,
    )
    expect(screen.getByText('zzq-first')).toBeInTheDocument()
    expect(screen.queryByText('zzq-second')).not.toBeInTheDocument()
  })

  it('shows the resolving indicator only while resolving', () => {
    const resolving = i18nT('pages.chat.chatNavPanel.resolving')
    const { rerender } = render(
      <ChatNavContent links={[]} sections={[]} onScrollToSection={() => {}} resolving />,
    )
    expect(screen.getByText(resolving)).toBeInTheDocument()
    rerender(<ChatNavContent links={[]} sections={[]} onScrollToSection={() => {}} />)
    expect(screen.queryByText(resolving)).not.toBeInTheDocument()
  })

  it('numbers outline rows from 1 and reports the display index on click', () => {
    const onScrollToSection = vi.fn()
    render(
      <ChatNavContent
        links={[]}
        sections={[
          section({ label: 'zzq-one', displayIdx: 4 }),
          section({ label: 'zzq-two', displayIdx: 9, msgIdx: 2 }),
        ]}
        onScrollToSection={onScrollToSection}
      />,
    )
    expect(screen.getByText('1.')).toBeInTheDocument()
    expect(screen.getByText('2.')).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('zzq-two'))
    expect(onScrollToSection).toHaveBeenCalledWith(9)
  })
})
