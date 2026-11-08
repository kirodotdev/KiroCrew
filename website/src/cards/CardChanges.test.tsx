/**
 * What a card changes reads in ONE form per row: a list edit as its +/− items
 * only (never also the whole list before → after), a scalar as old → new. A
 * list setting's card is titled and labelled for what the person will see.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import { effortLabel } from '../lib/effort'
import type { Card } from '../api/cards'
import { CardChanges, FIELD_LABELS, changeLabel, formatCardValue } from './CardChanges'
import { fmtCron } from '../utils/cronUtils'
import { cardChanges, cardTitle, scopeRepeatsTitle } from './cardRegistry'

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

  it('shows a workflow card script whole, as code, and its diff one marked row per line', () => {
    const script = 'META = {"name": "x"}\nasync def workflow(ctx):\n    return 1'
    // Source lines that start with the diff marks themselves stay their own rows.
    const diff = ' async def workflow(ctx):\n-    return 0\n+    return 1\n--- never delete\n+++ notes)'
    render(<CardChanges changes={[
      { field: 'workflow_command', label: 'Command', after: '/workflow x' },
      { field: 'workflow_diff', label: 'Changes to the script', after: diff },
      { field: 'workflow_source', label: 'Script', after: script },
    ]} />)
    const blocks = screen.getAllByTestId('change-card-code')
    expect(blocks.map(b => b.tagName)).toEqual(['PRE'])
    expect(blocks[0].className).toContain('font-mono')
    const codeLines = within(blocks[0]).getAllByTestId('change-card-code-line')
    expect(codeLines.map(l => within(l).getByTestId('change-card-line-text').textContent)).toEqual(script.split('\n'))
    const rows = screen.getAllByTestId('change-card-diff-line')
    expect(rows.map(r => r.dataset.kind)).toEqual(['context', 'remove', 'add', 'remove', 'add'])
    expect(rows[3].lastElementChild?.textContent).toBe('-- never delete')
    expect(rows[4].lastElementChild?.textContent).toBe('++ notes)')
    expect(rows[2].className).toContain('bg-ok-subtle')
    expect(rows[1].className).toContain('bg-danger-subtle')
    // The mark sits in its own gutter, apart from the line text beside it.
    expect(rows[2].firstElementChild?.textContent).toBe('+')
    expect(rows[2].firstElementChild?.getAttribute('aria-hidden')).toBe('true')
    expect(rows[2].textContent).toContain(L('added'))
    expect(rows[1].textContent).toContain(L('removed'))
    for (const k of ['field_workflow_command', 'field_workflow_diff', 'field_workflow_source']) expect(screen.getByText(L(k))).toBeTruthy()
    expect(screen.getByText('/workflow x')).toBeTruthy()
  })

  it('keeps every workflow script and diff line on one numbered row, scrolling sideways', () => {
    // Plain spaces push the tail of line 2 far right; wrapped, it would start a
    // row of its own at column 0 and read as a top-level statement.
    const tail = `pass${' '.repeat(150)}; await ctx.agent("confirm")`
    const script = `if ctx.args.get("never"):\n    ${tail}\nreturn 1`
    render(<CardChanges changes={[
      { field: 'workflow_diff', label: 'Changes to the script', after: ` if ctx.args.get("never"):\n+    ${tail}` },
      { field: 'workflow_source', label: 'Script', after: script },
    ]} />)
    const block = screen.getByTestId('change-card-code')
    const lines = within(block).getAllByTestId('change-card-code-line')
    expect(lines).toHaveLength(3)
    expect(lines.map(l => within(l).getByTestId('change-card-line-number').textContent)).toEqual(['1', '2', '3'])
    for (const l of lines) {
      expect(within(l).getByTestId('change-card-line-number').getAttribute('aria-hidden')).toBe('true')
    }
    const texts = [
      ...within(block).getAllByTestId('change-card-line-text'),
      ...within(screen.getByTestId('change-card-diff')).getAllByTestId('change-card-line-text'),
    ]
    expect(texts).toHaveLength(5)
    for (const t of texts) {
      expect(t.className).toContain('whitespace-pre')
      expect(t.className).not.toContain('whitespace-pre-wrap')
      expect(t.className).not.toContain('overflow-wrap')
    }
    expect(texts[1].textContent).toBe(`    ${tail}`)
    // Sideways only: no height cap, so no line ever sits below the box's bottom edge.
    for (const box of [block, screen.getByTestId('change-card-diff')]) {
      expect(box.className).toContain('overflow-x-auto')
      expect(box.className).not.toMatch(/max-h-|overflow-auto|overflow-y-auto/)
    }
    // The +/− mark sits behind a border, apart from script text that starts with -- or ++.
    for (const mark of screen.getAllByTestId('change-card-diff-mark')) expect(mark.className).toContain('border-r')
  })

  it('draws each hunk separator as a labelled gap row, never as a script line', () => {
    const diff = [
      '\u22ef lines 7-9 \u22ef',
      '     if ctx.args.get("confirmed"):',
      '-        await ctx.agent("old")',
      '+        await ctx.agent("new")',
      '\u22ef lines 27-29 \u22ef',
      '     return 1',
      '+    await purge()',
      // A script line that merely looks like a separator stays a script line.
      ' # \u22ef lines 1-2 \u22ef',
    ].join('\n')
    render(<CardChanges changes={[{ field: 'workflow_diff', label: 'Changes to the script', after: diff }]} />)
    const gaps = screen.getAllByRole('separator')
    expect(gaps).toHaveLength(2)
    expect(gaps.map(g => g.dataset.testid)).toEqual(['change-card-diff-gap', 'change-card-diff-gap'])
    expect(gaps[1].getAttribute('aria-label')).toBe(L('diff_gap_label', { start: '27', end: '29' }))
    expect(gaps[1]).toHaveTextContent(L('diff_gap', { start: '27', end: '29' }))
    // Set apart from code: its own face, muted and dashed, outside the +/− rows.
    for (const cls of ['font-sans', 'text-muted', 'border-dashed', 'border-border']) expect(gaps[0].className).toContain(cls)
    expect(gaps[0].className).not.toContain('font-mono')
    expect(within(gaps[0]).queryByTestId('change-card-line-text')).toBeNull()
    const rows = screen.getAllByTestId('change-card-diff-line')
    expect(rows.map(r => r.dataset.kind)).toEqual(['context', 'remove', 'add', 'context', 'add', 'context'])
    expect(rows[5].lastElementChild?.textContent).toBe('# \u22ef lines 1-2 \u22ef')
    // The gap sits between the two hunks, in document order.
    const all = Array.from(screen.getByTestId('change-card-diff').querySelectorAll(
      '[data-testid="change-card-diff-gap"], [data-testid="change-card-diff-line"]',
    ))
    expect(all.map(n => (n as HTMLElement).dataset.testid)).toEqual([
      'change-card-diff-gap', 'change-card-diff-line', 'change-card-diff-line', 'change-card-diff-line',
      'change-card-diff-gap', 'change-card-diff-line', 'change-card-diff-line', 'change-card-diff-line',
    ])
  })

  it('draws no gap row for a hunk that opens at line 1, since nothing above it is hidden', () => {
    const diff = ['\u22ef lines 1-3 \u22ef', ' a = 1', '-b = 2', '+b = 3'].join('\n')
    render(<CardChanges changes={[{ field: 'workflow_diff', label: 'Changes to the script', after: diff }]} />)
    expect(screen.queryByRole('separator')).toBeNull()
    expect(screen.getAllByTestId('change-card-diff-line').map(r => r.dataset.kind)).toEqual(['context', 'remove', 'add'])
  })

  it('says on screen that a code box hides lines past its right edge', () => {
    const proto = HTMLElement.prototype
    const saved = {
      scrollWidth: Object.getOwnPropertyDescriptor(proto, 'scrollWidth'),
      clientWidth: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
    }
    Object.defineProperty(proto, 'scrollWidth', { configurable: true, get: () => 900 })
    Object.defineProperty(proto, 'clientWidth', { configurable: true, get: () => 300 })
    try {
      render(<CardChanges changes={[
        { field: 'workflow_source', label: 'Script', after: 'X = 1  # ' + 'a'.repeat(140) },
        { field: 'workflow_diff', label: 'Changes to the script', after: '-X = 0\n+X = 1  # ' + 'a'.repeat(140) },
      ]} />)
      expect(screen.getByTestId('change-card-code').dataset.fadeRight).toBe('true')
      expect(screen.getByTestId('change-card-diff').dataset.fadeRight).toBe('true')
      const notes = screen.getAllByTestId('change-card-code-overflow')
      expect(notes).toHaveLength(2)
      for (const n of notes) expect(n).toHaveTextContent(L('code_scrolls'))
    } finally {
      for (const [k, d] of Object.entries(saved)) {
        if (d) Object.defineProperty(proto, k, d)
        else delete (proto as unknown as Record<string, unknown>)[k]
      }
    }
  })

  it('shows no overflow line when every code line fits', () => {
    render(<CardChanges changes={[{ field: 'workflow_source', label: 'Script', after: 'X = 1' }]} />)
    expect(screen.getByTestId('change-card-code').dataset.fadeRight).toBeUndefined()
    expect(screen.queryByTestId('change-card-code-overflow')).toBeNull()
  })

  it('names a hunk that only removes lines by the line it removes after', () => {
    const diff = ['\u22ef after line 10 \u22ef', '-    await purge()'].join('\n')
    render(<CardChanges changes={[{ field: 'workflow_diff', label: 'Changes to the script', after: diff }]} />)
    const gap = screen.getByRole('separator')
    expect(gap.getAttribute('aria-label')).toBe(L('diff_gap_after_label', { line: '10' }))
    expect(gap).toHaveTextContent(L('diff_gap_after', { line: '10' }))
    const rows = screen.getAllByTestId('change-card-diff-line')
    expect(rows.map(r => r.dataset.kind)).toEqual(['remove'])
  })

  it('shows every rule in full and each imported cron as arriving paused', () => {
    render(<CardChanges changes={[
      { field: 'rules', label: 'Rules', after: 'Never push to main.\nAsk before deleting.' },
      { field: 'import_crons', label: 'Crons', after: [{ name: 'weekly', cron_expr: '0 9 * * 1' }] },
      { field: 'import_skills', label: 'Skills', after: [{ name: 'notes', source: 'app:release-tools' }] },
      { field: 'carried_skills', label: 'x', after: [{ name: 'drafts', files: [{ path: 'SKILL.md' }, { path: 'a.py' }] }] },
      { field: 'carried_scripts', label: 'x', after: [{ file: 'run.py' }] },
    ]} />)
    const rules = screen.getByText(/Never push to main\./)
    expect(rules.textContent).toBe('Never push to main.\nAsk before deleting.')
    expect(rules.className).toContain('whitespace-pre-wrap')
    for (const k of ['field_rules', 'field_import_crons', 'field_import_skills', 'field_carried_skills', 'field_carried_scripts']) expect(screen.getByText(L(k))).toBeTruthy()
    expect(screen.getByText(/weekly/).textContent).toContain('(arrives paused)')
    render(<CardChanges changes={[{ field: 'import_crons', label: 'Crons', after: [{ name: 'daily', cron_expr: '0 8 * * *', message: 'Post the summary', timezone: 'Europe/Paris' }] }]} />)
    const daily = screen.getByText(/daily/).textContent ?? ''
    expect(daily).toContain('Post the summary')
    expect(daily).toContain(L('import_cron_timezone', { tz: 'Europe/Paris' }))
    expect(screen.getByText(/notes/).textContent).toContain('release-tools')
    expect(screen.getByText(L('carried_skill_files', { name: 'drafts', count: 2 }))).toBeTruthy()
    expect(screen.getByText('run.py')).toBeTruthy()
  })

  it('shows the "request tools" flag as off → on in plain words, with what it means', () => {
    render(<CardChanges changes={[{ field: 'dynamic_capabilities', label: 'dynamic_capabilities', before: false, after: true }]} />)
    const value = screen.getByTestId('change-card-value').textContent ?? ''
    expect(value).toContain(L('tool_access_not_allowed'))
    expect(value).toContain(L('tool_access_allowed'))
    expect(value).not.toMatch(/true|false/)
    expect(screen.getByText(L('field_dynamic_capabilities'))).toBeTruthy()
    expect(screen.getByTestId('change-card-hint')).toHaveTextContent(L('dynamic_capabilities_hint'))
    expect(L('field_dynamic_capabilities')).not.toMatch(/preview|dynamic/i)
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

  it('treats the Affects row as a repeat of the title only for the same one member, as a whole word', () => {
    const own = (members: string[], count = members.length) => ({ count, members, only_this_member: true })
    expect(scopeRepeatsTitle('Add a goal for "Scout"', own(['Scout']))).toBe(true)
    expect(scopeRepeatsTitle('给 Scout 设定目标', own(['Scout']))).toBe(true)
    expect(scopeRepeatsTitle('Add a goal for Alex', own(['Al']))).toBe(false)
    // A template's users: the gateway does not mark them as the card's subject,
    // so a template named like its one crewmate still shows who is affected.
    expect(scopeRepeatsTitle('Change template “scout”', { count: 1, members: ['scout'] })).toBe(false)
    expect(scopeRepeatsTitle('Add a goal for Scout', { count: 2, members: ['Scout', 'Pebble'] })).toBe(false)
    expect(scopeRepeatsTitle('Add a goal for Scout', { count: 2, members: ['Scout'] })).toBe(false)
    expect(scopeRepeatsTitle('Shorter replies', { count: 1, members: ['Scout'] })).toBe(false)
    expect(scopeRepeatsTitle('Add a goal for Scout', undefined)).toBe(false)
  })

  it('shows an add as its new value alone, never "(empty) →", and keeps old → new for an edit', () => {
    const adds = [null, '', []].map((before, i) => ({ field: 'goal', label: `Goal ${i + 1}`, number: i + 1, before, after: `Ship v${i}` }))
    render(<CardChanges changes={[...adds, { field: 'goal', label: 'Goal 9', number: 9, before: 'Old goal', after: 'New goal' }]} />)
    const values = screen.getAllByTestId('change-card-value').map(v => v.textContent ?? '')
    for (const [i, text] of values.slice(0, 3).entries()) {
      expect(text).toBe(`Ship v${i}`)
      expect(text).not.toContain(L('empty_value'))
    }
    expect(values[3]).toContain('Old goal')
    expect(values[3]).toContain('→')
    expect(values[3]).toContain('New goal')
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
    expect(line.textContent).toContain(L('addition_no_new_grants'))
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
    expect(line.textContent).not.toContain(L('addition_no_new_grants'))
  })
})
