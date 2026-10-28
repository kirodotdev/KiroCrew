/**
 * A compiled UserMessage follows a language switch.
 *
 * UserMessage is on the React Compiler opt-in list (react-compiler.config.mjs),
 * and vitest runs listed files compiled. The compiler caches each string on the
 * reactive inputs it reads, so the component takes its strings from
 * `useTranslation()`'s `t`, whose identity changes on a switch. The test keeps
 * every prop referentially identical across the switch, so only that input can
 * move the action labels to the new language.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'

import UserMessage from '../pages/chat/UserMessage'
import { i18next } from '../i18n'
import { i18nT } from '../i18n/t'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

const renderContent = (content: string) => <span data-testid="content">{content}</span>
const onReplyInThread = vi.fn()

/** Stands in for the content chips: it translates and subscribes to nothing. */
function UnsubscribedChip() {
  return <span data-testid="chip">{i18nT('pages.chat.thread.reply_in_thread')}</span>
}
const renderWithChip = (content: string) => <><span>{content}</span><UnsubscribedChip /></>


afterEach(async () => {
  await act(async () => { await i18next.changeLanguage('en') })
})

describe('UserMessage under a language switch', () => {
  it('relabels its actions in the new language with unchanged props', async () => {
    const tree = <UserMessage content="hi" renderContent={renderContent} onReplyInThread={onReplyInThread} />
    const { rerender } = render(tree)
    const englishReply = i18nT('pages.chat.thread.reply_in_thread')
    expect(screen.getByTestId('reply-in-thread')).toHaveAttribute('aria-label', englishReply)

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(tree)

    const chineseReply = i18nT('pages.chat.thread.reply_in_thread')
    expect(chineseReply).not.toBe(englishReply)
    expect(screen.getByTestId('reply-in-thread')).toHaveAttribute('aria-label', chineseReply)
  })

  it('rebuilds the renderContent output, so an unsubscribed child relabels too', async () => {
    const tree = <UserMessage content="hi" renderContent={renderWithChip} />
    const { rerender } = render(tree)
    const english = i18nT('pages.chat.thread.reply_in_thread')
    expect(screen.getByTestId('chip')).toHaveTextContent(english)

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(tree)

    const chinese = i18nT('pages.chat.thread.reply_in_thread')
    expect(chinese).not.toBe(english)
    expect(screen.getByTestId('chip')).toHaveTextContent(chinese)
  })
})
