import { afterAll, describe, expect, it } from 'vitest'
import { act, render, screen } from '@testing-library/react'

import PromptLengthNotice from '../components/PromptLengthNotice'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
// `/all` registers the 11 authored catalogs; `../i18n` alone registers English.
import { i18next } from '../i18n/all'

// A 100-token window: 400 ASCII characters is exactly the limit.
const WINDOW = 100

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('PromptLengthNotice', () => {
  it('shows nothing visible and announces nothing under the threshold', () => {
    render(<PromptLengthNotice value={'a'.repeat(300)} blocks={[]} contextWindowTokens={WINDOW} />)
    expect(screen.queryByTestId('prompt-length-notice')).not.toBeInTheDocument()
    expect(screen.getByTestId('prompt-length-live')).toHaveTextContent('')
  })

  it('warns near the limit with the size and the limit', () => {
    render(<PromptLengthNotice value={'a'.repeat(380)} blocks={[]} contextWindowTokens={WINDOW} />)
    const notice = screen.getByTestId('prompt-length-notice')
    expect(notice).toHaveAttribute('data-level', 'near')
    expect(notice.className).toContain('text-warn')
    expect(notice).toHaveTextContent('~95 of the 100 tokens')
    expect(screen.getByTestId('prompt-length-live')).toHaveTextContent('Prompt is close to its length limit.')
    expect(screen.getByTestId('prompt-length-live')).toHaveAttribute('aria-live', 'polite')
  })

  it('turns into an error over the limit and says how far over', () => {
    render(<PromptLengthNotice value={'a'.repeat(480)} blocks={[]} contextWindowTokens={WINDOW} />)
    const notice = screen.getByTestId('prompt-length-notice')
    expect(notice).toHaveAttribute('data-level', 'over')
    expect(notice.className).toContain('text-danger')
    expect(notice).toHaveTextContent('~20 over the 100-token limit')
    expect(screen.getByTestId('prompt-length-live')).toHaveTextContent('Prompt is over its length limit.')
  })

  it('counts the expanded paste content, not the collapsed chip', () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 1, content: 'b'.repeat(480) }
    const value = formatToken(block)
    expect(value.length).toBeLessThan(40)
    render(<PromptLengthNotice value={value} blocks={[block]} contextWindowTokens={WINDOW} />)
    expect(screen.getByTestId('prompt-length-notice')).toHaveAttribute('data-level', 'over')
  })

  it('does not count outer whitespace stripped by the send path', () => {
    const value = `${' '.repeat(1000)}${'a'.repeat(400)}${' '.repeat(1000)}`
    render(<PromptLengthNotice value={value} blocks={[]} contextWindowTokens={WINDOW} />)
    expect(screen.getByTestId('prompt-length-notice')).toHaveAttribute('data-level', 'near')
  })

  it('stays hidden when the context window is unknown', () => {
    render(<PromptLengthNotice value={'a'.repeat(100_000)} blocks={[]} contextWindowTokens={0} />)
    expect(screen.queryByTestId('prompt-length-notice')).not.toBeInTheDocument()
  })

  it('goes away when the prompt is trimmed back under the threshold', () => {
    const { rerender } = render(<PromptLengthNotice value={'a'.repeat(480)} blocks={[]} contextWindowTokens={WINDOW} />)
    expect(screen.getByTestId('prompt-length-notice')).toBeInTheDocument()
    rerender(<PromptLengthNotice value={'a'.repeat(100)} blocks={[]} contextWindowTokens={WINDOW} />)
    expect(screen.queryByTestId('prompt-length-notice')).not.toBeInTheDocument()
    expect(screen.getByTestId('prompt-length-live')).toHaveTextContent('')
  })

  it('never takes focus', () => {
    render(<PromptLengthNotice value={'a'.repeat(480)} blocks={[]} contextWindowTokens={WINDOW} />)
    const notice = screen.getByTestId('prompt-length-notice')
    expect(notice.querySelector('button, a, input, [tabindex]')).toBeNull()
    expect(notice).not.toHaveAttribute('tabindex')
    expect(document.activeElement).toBe(document.body)
  })

  // The component is memo()-wrapped and its props do not change on a language
  // switch, so without the useLanguageGeneration() subscription the line would
  // keep the previous catalog (see src/i18n/useLanguageGeneration.ts).
  it('updates the visible line after a language change, with props unchanged', async () => {
    render(<PromptLengthNotice value={'a'.repeat(380)} blocks={[]} contextWindowTokens={WINDOW} />)
    const notice = screen.getByTestId('prompt-length-notice')
    expect(notice).toHaveTextContent('~95 of the 100 tokens')

    await act(async () => { await i18next.changeLanguage('de') })
    expect(notice.textContent).toContain('Der Prompt umfasst')
    expect(notice.textContent).not.toContain('of the')
    expect(screen.getByTestId('prompt-length-live').textContent)
      .toBe(i18next.t('components.promptLength.sr_near'))
  })
})
