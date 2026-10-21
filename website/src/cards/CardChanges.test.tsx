/**
 * What a card changes reads in ONE form per row: a list edit as its +/− items
 * only (never also the whole list before → after), a scalar as old → new. A
 * list setting's card is titled and labelled for what the person will see.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import { effortLabel } from '../lib/effort'
import type { Card } from '../api/cards'
import { CardChanges, FIELD_LABELS, changeLabel, formatCardValue } from './CardChanges'
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
  it('labels a goal row with the number the Goals tab shows', () => {
    expect(changeLabel({ field: 'goal', label: 'Goal 2', number: 2 })).toBe(i18nT('pages.membersPage.goals_goal_number', { number: 2 }))
    expect(changeLabel({ field: 'goal', label: 'Goal' })).toBe(FIELD_LABELS.goal())
  })
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

  it("shows an approval mode in the picker's words, under its localized label", () => {
    render(<CardChanges changes={[{ field: 'approval_mode', label: 'approval_mode', before: 'trust', after: 'normal' }]} />)
    const value = screen.getByTestId('change-card-value').textContent ?? ''
    expect(value).toContain(i18nT('components.approvalModePicker.trust_label'))
    expect(value).toContain(i18nT('components.approvalModePicker.normal_label'))
    expect(value).not.toContain('trust')
    expect(screen.getByText(L('field_approval_mode'))).toBeTruthy()
    // The permission being approved is explained under the value.
    expect(screen.getByTestId('change-card-hint')).toHaveTextContent(L('approval_mode_hint_normal'))
  })

  it("shows an import card's sender and what it leaves out in the reader's language", () => {
    render(<CardChanges changes={[
      { field: 'sender', label: 'From', after: 'Ana' },
      { field: 'not_included', label: 'Not included', after: 'private' },
      { field: 'not_imported', label: 'Left out', after: ['crons', 'model'] },
      { field: 'import_approval', label: 'Approval', after: 'normal' },
      { field: 'template_auto_approve', label: 'x', after: [{ name: 'gh', host: 'api.example' }, { name: 'fs', command: 'uvx' }] },
    ]} />)
    expect(screen.getByText(`${L('reach_remote', { name: 'gh', host: 'api.example' })}; ${L('reach_local', { name: 'fs', command: 'uvx' })}`)).toBeTruthy()
    expect(screen.getByTestId('change-card-template-link').getAttribute('href')).toBe('/capabilities?tab=templates')
    for (const k of ['field_sender', 'field_not_included', 'not_included_private', 'field_not_imported', 'approval_hint_import']) expect(screen.getByText(L(k))).toBeTruthy()
    expect(screen.getByText(fmtList([L('left_crons'), L('left_model')]))).toBeTruthy()
    expect(screen.queryByText('private')).toBeNull()
  })

  it("shows a reasoning effort in the editor's words, and an unset one as inherited", () => {
    render(<CardChanges changes={[{ field: 'reasoning_effort', label: 'reasoning_effort', before: '', after: 'high' }]} />)
    const value = screen.getByTestId('change-card-value').textContent ?? ''
    expect(value).toContain(i18nT('pages.kiroCrewAgentsPage.inherited'))
    expect(value).toContain(effortLabel('high'))
    expect(value).not.toContain(L('empty_value'))
  })

  it('draws no hint under a row whose field has none', () => {
    render(<CardChanges changes={[{ field: 'description', label: 'description', before: 'old', after: 'new' }]} />)
    expect(screen.queryByTestId('change-card-hint')).toBeNull()
  })

  it('formats empty, scalar and object values', () => {
    for (const empty of [undefined, null, '']) expect(formatCardValue(empty)).toBe(L('empty_value'))
    expect(formatCardValue(3)).toBe('3')
    expect(formatCardValue(false)).toBe('false')
    expect(formatCardValue({ a: 1 })).toBe('{"a":1}')
    // A value JSON cannot write still reads as something, never throws.
    const loop: Record<string, unknown> = {}
    loop.self = loop
    expect(formatCardValue(loop)).toBe('[object Object]')
  })

  it("labels a row by its localized field, else by the gateway's label", () => {
    for (const field of Object.keys(FIELD_LABELS)) {
      expect(changeLabel({ field, label: 'gateway' })).toBe(L(`field_${field}`))
    }
    expect(changeLabel({ field: 'unknown_field', label: 'gateway' })).toBe('gateway')
    expect(changeLabel({ label: 'gateway' })).toBe('gateway')
  })

  it('shows added items as + chips and a new value with no before alone', () => {
    render(<CardChanges changes={[{ label: 'Tags', add: ['red'], remove: [] }, { label: 'Name', after: 'Radar' }]} />)
    expect(screen.getByTestId('change-card-items').textContent).toContain('red')
    const value = screen.getByTestId('change-card-value')
    expect(value.textContent).toContain('Radar')
    expect(value.textContent).not.toContain('→')
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

describe('CardChanges: a capabilities card in plain words', () => {
  const ADDITION = {
    label: 'weather',
    addition: { kind: 'server' as const, name: 'weather', origin: 'gateway' as const, auto_approve: ['weather_lookup'] },
  }
  const RAW = [
    { label: 'mcpServers', add: ['weather'], detail: true },
    { label: 'weather', after: 'python3 /home/someone/stub.py weather', detail: true },
  ]

  it('reads one plain line per addition, named for the crewmate, and keeps raw rows behind Details', async () => {
    const { CardAuthorContext } = await import('./cardAuthor')
    const { container } = render(
      <CardAuthorContext.Provider value="Ada">
        <CardChanges changes={[ADDITION, ...RAW]} />
      </CardAuthorContext.Provider>,
    )
    // Each raw row is drawn once, inside the Details, never in the main view.
    expect(container.textContent?.split('stub.py').length).toBe(2)
    expect(screen.getAllByTestId('change-card-items')).toHaveLength(1)
    const line = screen.getByTestId('change-card-addition')
    expect(line.textContent).toContain(L('addition_server', { name: 'Ada', server: 'weather' }))
    expect(line.textContent).toContain(L('addition_from_gateway'))
    expect(line.textContent).toContain(L('addition_runs_unasked', { tools: 'weather_lookup' }))
    expect(line.querySelector('strong')?.textContent).toBe('weather')
    expect(line.querySelector('code')?.textContent).toBe('weather_lookup')
    // The launch line and the raw sections are on the card, collapsed.
    const details = screen.getByTestId('change-card-details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('mcpServers')
    expect(details.textContent).toContain('stub.py')
    expect(line.textContent).not.toContain('stub.py')
  })

  it('says so when nothing runs without asking, and names a registry install', () => {
    render(
      <CardChanges
        changes={[{ label: 'gh', addition: { kind: 'server', name: 'gh', origin: 'registry', registry_id: 'io/gh', auto_approve: [] } }]}
      />,
    )
    const line = screen.getByTestId('change-card-addition')
    expect(line.textContent).toContain(L('addition_asks_every_time'))
    expect(line.textContent).toContain('io/gh')
    expect(screen.queryByTestId('change-card-details')).toBeNull()
  })

  it('says every tool runs without asking when the card grants the whole server', () => {
    render(
      <CardChanges
        changes={[{ label: 'weather', addition: { kind: 'server', name: 'weather', origin: 'gateway', auto_approve: [], auto_approve_all: true } }]}
      />,
    )
    const line = screen.getByTestId('change-card-addition')
    expect(line.textContent).toContain(L('addition_runs_all_unasked'))
    expect(line.textContent).not.toContain(L('addition_asks_every_time'))
  })
})
