import { describe, expect, it } from 'vitest'
import { deriveFollowUpOptions } from '../../app-sdk/protocol'
import type { ChatMessage } from '../../types'
import { asksForCard, crewmateFollowUps } from './crewmateChips'

const said = (content: string): ChatMessage => ({ role: 'assistant', content, cls: '', ts: '2026-10-29T00:00:00Z' })

describe('crewmateFollowUps', () => {
  it('drops the H4 chip that asks for a card no kind can raise', () => {
    const d = deriveFollowUpOptions(
      [said('A safety rule blocked the install.\n[OPTIONS: Ask for a card to let you install it | I installed Rust, check the task]')],
      false,
    )
    expect(d.followUpOptions).toContain('Ask for a card to let you install it')
    expect(crewmateFollowUps(d).followUpOptions).toEqual(['I installed Rust, check the task'])
  })

  it('offers nothing when every chip asks for a card', () => {
    const d = deriveFollowUpOptions([said('x\n[OPTIONS: Raise a change card | 帮我弹出变更卡片]')], false)
    const out = crewmateFollowUps(d)
    expect(out.followUpOptions).toEqual([])
    expect(out.followUpSourceKey).toBeNull()
  })

  it('returns the same derivation when nothing is dropped', () => {
    const d = deriveFollowUpOptions([said('x\n[OPTIONS: Show me the install line | Show my credit cards]')], false)
    expect(crewmateFollowUps(d)).toBe(d)
  })

  it('matches card asks, not the user\'s own cards', () => {
    expect(asksForCard('Ask for a card to let you install it')).toBe(true)
    expect(asksForCard('Raise a change card for the install')).toBe(true)
    expect(asksForCard('Use propose_change for this')).toBe(true)
    expect(asksForCard('帮我弹出变更卡片')).toBe(true)
    expect(asksForCard('Show my credit cards')).toBe(false)
    expect(asksForCard('Add a new card to the board')).toBe(false)
    expect(asksForCard('Block the card I lost')).toBe(false)
    expect(asksForCard('신용카드 목록 정리해줘')).toBe(false)
    expect(asksForCard('整理我的银行卡片')).toBe(false)
  })

  it.each([
    'Создай карточку изменения',
    'Änderungskarte anfordern',
    'Peça um cartão de aprovação',
    'Envie os cartões de permissão',
    'Demande une carte de validation',
    'Pide una tarjeta de aprobación',
    'Richiedi una scheda di approvazione',
    '承認カードを出してください',
    '승인 카드를 띄워 주세요',
  ])('drops a chip that names one of our cards in another language: %s', label => {
    expect(asksForCard(label)).toBe(true)
  })

  it.each([
    'Give me the card number',
    'Make the cards bigger',
    'Change card colors on the board',
    'クレジットカードを表示して',
    'Muestra la tarjeta de crédito',
    'Mostra la scheda del prodotto',
    'Ask for a refund on the card',
  ])('keeps a chip that only mentions a card: %s', label => {
    expect(asksForCard(label)).toBe(false)
  })
})
