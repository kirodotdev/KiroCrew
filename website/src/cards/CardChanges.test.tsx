/**
 * What a card changes reads in ONE form per row: a list edit as its +/− items
 * only (never also the whole list before → after), a scalar as old → new. A
 * list setting's card is titled and labelled for what the person will see.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import type { Card } from '../api/cards'
import { CardChanges, formatCardValue } from './CardChanges'
import { fmtCron } from '../utils/cronUtils'
import { cardChanges, cardTitle } from './cardRegistry'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.changeCards.${k}`, v)
const LIST_ROW = { label: 'Selectable Models', before: ['other-2', 'fable-1'], after: ['other-2'], add: [], remove: ['fable-1'] }

const listCard = (params: Record<string, unknown>): Pick<Card, 'kind' | 'params' | 'title' | 'changes'> => ({
  kind: 'setting.change',
  title: 'gateway title',
  params,
  changes: [LIST_ROW],
})

describe('CardChanges', () => {
  it('formats a string list as a list and an empty one as empty', () => {
    expect(formatCardValue(['a-1', 'b-2'])).toBe(fmtList(['a-1', 'b-2'], { style: 'narrow' }))
    expect(formatCardValue([])).toBe(i18nT('components.changeCards.empty_value'))
    // A non-string array keeps the JSON fallback.
    expect(formatCardValue([1, 2])).toBe('[1,2]')
  })

  it('shows a list edit as its item chip only, not also the lists before and after', () => {
    render(<CardChanges changes={[LIST_ROW]} />)
    expect(screen.getByTestId('change-card-items').textContent).toContain('fable-1')
    expect(screen.queryByTestId('change-card-value')).toBeNull()
    expect(screen.queryByText(fmtList(['other-2', 'fable-1'], { style: 'narrow' }))).toBeNull()
    expect(screen.queryByText(L('empty_value'))).toBeNull()
    expect(screen.queryByText('→')).toBeNull()
  })

  it('shows a cron row the way the Schedule page does, under its localized label', () => {
    render(<CardChanges changes={[{ field: 'cron_expr', label: 'When', before: '0 8 * * *', after: '0 9 * * 1-5' }]} />)
    const value = screen.getByTestId('change-card-value')
    expect(value.textContent).toContain(fmtCron('0 9 * * 1-5'))
    expect(value.textContent).toContain(fmtCron('0 8 * * *'))
    expect(value.textContent).not.toContain('0 9 * * 1-5')
    expect(screen.getByText(i18nT('components.changeCards.field_cron_expr'))).toBeTruthy()
  })

  it('shows a scalar as old → new', () => {
    render(<CardChanges changes={[{ label: 'Reply length', before: 'standard', after: 'brief' }]} />)
    expect(screen.getByTestId('change-card-value').textContent).toContain('standard')
    expect(screen.getByTestId('change-card-value').textContent).toContain('brief')
    expect(screen.queryByTestId('change-card-items')).toBeNull()
  })
})

describe('list setting wording', () => {
  it('titles the hidden-models list by what the person will see, per op', () => {
    const remove = listCard({ setting_id: 'chat.selectable-models', op: 'remove', item: 'fable-1' })
    expect(cardTitle(remove)).toBe(L('hidden_models_show', { item: 'fable-1' }))
    const add = listCard({ path: 'dashboard.model_picker_hidden_models', op: 'add', item: 'fable-1' })
    expect(cardTitle(add)).toBe(L('hidden_models_hide', { item: 'fable-1' }))
    // The row names the list it edits, not the Settings control's label.
    expect(cardChanges(remove)[0].label).toBe(L('hidden_models_label'))
  })

  it('falls back to "Add X to" / "Remove X from" the list label', () => {
    const other = (op: string) => listCard({ setting_id: 'chat.other-list', op, item: 'x' })
    expect(cardTitle(other('add'))).toBe(L('list_title_add', { item: 'x', label: 'Selectable Models' }))
    expect(cardTitle(other('remove'))).toBe(L('list_title_remove', { item: 'x', label: 'Selectable Models' }))
    expect(L('list_title_remove', { item: 'x', label: 'L' })).toContain(' from ')
    expect(cardChanges(other('add'))[0].label).toBe('Selectable Models')
  })

  it('keeps the gateway title for any other card', () => {
    expect(cardTitle({ kind: 'setting.change', title: 'Shorter replies', params: { value: 'brief' }, changes: [] })).toBe('Shorter replies')
  })
})
