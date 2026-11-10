/**
 * The chat knowledge chip names its token count as one counted message.
 *
 * It was the number followed by a bare "tokens" word, so English read
 * "1 tokens" and no language could put its own plural or word order on it.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
// `/all` registers every authored catalog; `../../i18n` registers English only.
import { i18next } from '../../i18n/all'
import { KnowledgeBubbleChip } from './ChatPageMessageContent'

const chip = (tokens: number) => {
  render(<KnowledgeBubbleChip knowledge={{ items: 1, tokens, titles: [] }} />)
  return screen.getByRole('button').textContent ?? ''
}

describe('knowledge chip token count', () => {
  afterEach(async () => {
    cleanup()
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('reads "1 token" for one token and "1,200 tokens" for more in English', () => {
    const one = chip(1)
    expect(one).toContain('1 token')
    expect(one).not.toContain('1 tokens')
    cleanup()
    expect(chip(1200)).toContain('1,200 tokens')
  })

  it('uses the Russian count forms', async () => {
    await act(async () => { await i18next.changeLanguage('ru') })
    const one = chip(1)
    expect(one).toContain('1 токен')
    expect(one).not.toContain('токенов')
    cleanup()
    expect(chip(3)).toContain('3 токена')
  })

  it('lets Korean put the noun before the number', async () => {
    await act(async () => { await i18next.changeLanguage('ko') })
    expect(chip(5)).toContain('토큰 5개')
  })
})
