// @vitest-environment jsdom
/**
 * The `org-chart` page's Open / Closed / All filter, driven rather than read.
 *
 * The page's real script runs in a real DOM against the field bag the host hands it,
 * the way `orgChartFoldPage.test.ts` drives the fold toggles. jsdom lays nothing out,
 * so every case reads the page's own state (text, attributes, element counts).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(
  __dirname,
  '../../../src/kiro_crew/dashboard_templates/builtin/org-chart/template.html',
)
const RAW = readFileSync(TEMPLATE, 'utf-8')

function halves() {
  const doc = new DOMParser().parseFromString(RAW, 'text/html')
  const nodes = Array.from(doc.querySelectorAll('script'))
  const script = nodes.map((node) => node.textContent ?? '').join('\n')
  for (const node of nodes) node.remove()
  return { script, body: doc.body }
}

const NOW = Date.parse('2026-10-06T00:40:00Z')

function task(id: string, state = 'open', extra: Record<string, unknown> = {}) {
  return {
    item_id: id,
    title: `task ${id}`,
    state,
    status: 'progress',
    summary: `said ${id}`,
    spender: `w-${id}`,
    credits: 1,
    credits_reported: true,
    last_report_at: new Date(NOW - 60_000).toISOString(),
    created_at: new Date(NOW - 3_600_000).toISOString(),
    ...extra,
  }
}

function board(
  id: string,
  tasks: Array<Record<string, unknown>>,
  parent: { board: string; item_id: string } | null = null,
) {
  return {
    id,
    goal: `goal of ${id}`,
    round: 1,
    parent,
    total: tasks.length,
    accepted: 0,
    open: tasks.length,
    needs_you: 0,
    credits: 1,
    credits_reported: true,
    tasks,
    tasks_omitted: 0,
    series: [],
  }
}

/**
 * lead:     a open, b accepted (runs sub-open), c rejected (runs sub-closed),
 *           e abandoned (runs sub-mixed)
 * sub-open: d open
 * sub-closed: f accepted
 * sub-mixed: g open, h rejected
 * other:    a second lead holding only closed work (i abandoned)
 */
function crew() {
  return [
    board('lead', [task('a'), task('b', 'accepted'), task('c', 'rejected'), task('e', 'abandoned')]),
    board('sub-open', [task('d')], { board: 'lead', item_id: 'b' }),
    board('sub-closed', [task('f', 'accepted')], { board: 'lead', item_id: 'c' }),
    board('sub-mixed', [task('g'), task('h', 'rejected')], { board: 'lead', item_id: 'e' }),
    board('other', [task('i', 'abandoned')]),
  ]
}

const added: Array<[string, EventListenerOrEventListenerObject]> = []

function mount(items: unknown[], locale?: string) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields: { items, omitted: 0, spenders_omitted: 0, last_entry_at: new Date(NOW).toISOString() },
    agentic: [],
    seq: 1,
    stale: false,
    missing: [],
    written_at: {},
    ...(locale ? { locale } : {}),
  }
  const { script, body } = halves()
  document.body.replaceChildren(...Array.from(body.childNodes))
  const spy = vi.spyOn(window, 'addEventListener')
  try {
    new Function(script)()
  } finally {
    for (const [type, listener] of spy.mock.calls) if (listener) added.push([type, listener])
    spy.mockRestore()
  }
}

const pick = (mode: string) =>
  document.querySelector<HTMLButtonElement>(`button[data-oc-filter="${mode}"]`)!
const count = (mode: string) => pick(mode).querySelector('[data-oc-count]')!.textContent
const graphText = () => document.getElementById('oc-graph')!.textContent ?? ''
const cardNames = () =>
  Array.from(document.querySelectorAll('#oc-cards .oc-card .oc-facts span:first-child b')).map(
    (n) => n.textContent,
  )
const leads = () => document.querySelectorAll('#oc-graph .oc-chip.oc-is-lead').length
const subs = () => document.querySelectorAll('#oc-graph .oc-chip.oc-is-sub').length

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  for (const [type, listener] of added.splice(0)) window.removeEventListener(type, listener)
  vi.useRealTimers()
})

describe('org-chart: the Open / Closed / All filter', () => {
  it('starts on All, and draws every task as the page did before the filter', () => {
    mount(crew())
    expect(pick('all').getAttribute('aria-pressed')).toBe('true')
    expect(pick('open').getAttribute('aria-pressed')).toBe('false')
    expect(pick('closed').getAttribute('aria-pressed')).toBe('false')
    expect(leads()).toBe(2)
    expect(subs()).toBe(3)
    expect(cardNames()).toHaveLength(9)
  })

  it('shows each choice with the number of tasks it holds', () => {
    mount(crew())
    expect(count('open')).toBe('3')
    expect(count('closed')).toBe('6')
    expect(count('all')).toBe('9')
  })

  it('uses real buttons that carry their own word', () => {
    mount(crew())
    const labels = ['open', 'closed', 'all'].map((m) => {
      expect(pick(m).tagName).toBe('BUTTON')
      expect(pick(m).type).toBe('button')
      return pick(m).textContent
    })
    expect(labels).toEqual(['Open3', 'Closed6', 'All9'])
  })

  it('Open keeps open tasks and the leads and sub-leads on the path to them', () => {
    mount(crew())
    pick('open').click()
    expect(pick('open').getAttribute('aria-pressed')).toBe('true')
    expect(pick('all').getAttribute('aria-pressed')).toBe('false')
    const g = graphText()
    for (const t of ['task a', 'task d', 'task g']) expect(g).toContain(t)
    for (const t of ['task f', 'task h', 'task i']) expect(g).not.toContain(t)
    // b (accepted) and e (abandoned) stay because an open task sits under them.
    expect(g).toContain('task b')
    expect(g).toContain('task e')
    // c (rejected) runs only closed work, so it goes; so does the board with only closed work.
    expect(g).not.toContain('task c')
    expect(g).not.toContain('goal of other')
    expect(leads()).toBe(1)
    expect(subs()).toBe(2)
    expect(cardNames().sort()).toEqual(['task a', 'task d', 'task g'])
  })

  it('Closed keeps closed tasks, and drops the board that holds none', () => {
    mount(crew())
    pick('closed').click()
    const g = graphText()
    for (const t of ['task b', 'task c', 'task e', 'task f', 'task h', 'task i']) expect(g).toContain(t)
    for (const t of ['task a', 'task d', 'task g']) expect(g).not.toContain(t)
    // sub-open holds nothing closed, so b is a plain worker row; sub-closed and sub-mixed stay.
    expect(leads()).toBe(2)
    expect(subs()).toBe(2)
    expect(cardNames()).toHaveLength(6)
  })

  it('goes back to the same page when All is pressed again', () => {
    mount(crew())
    const before = document.getElementById('oc-graph')!.innerHTML
    const cardsBefore = document.getElementById('oc-cards')!.innerHTML
    pick('open').click()
    pick('all').click()
    expect(document.getElementById('oc-graph')!.innerHTML).toBe(before)
    expect(document.getElementById('oc-cards')!.innerHTML).toBe(cardsBefore)
  })

  it('shows a plain line, not a blank graph, when the choice holds nothing', () => {
    mount([board('only', [task('a', 'accepted')])])
    pick('open').click()
    expect(graphText()).toContain('No open task to show.')
    expect(document.querySelectorAll('#oc-cards .oc-card')).toHaveLength(0)
    pick('closed').click()
    expect(graphText()).not.toContain('No open task to show.')
    expect(graphText()).toContain('task a')
  })

  it('keeps the choice across a host push, and keeps focus on the pressed button', () => {
    mount(crew())
    pick('open').focus()
    pick('open').click()
    window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew:fields' } }))
    vi.advanceTimersByTime(1)
    expect(pick('open').getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement).toBe(pick('open'))
    expect(graphText()).not.toContain('task i')
  })

  it('leaves the per-group Hide control working under a filter', () => {
    mount(crew())
    pick('open').click()
    const lead = document.querySelector<HTMLButtonElement>('button.oc-fold')!
    lead.click()
    expect(lead.getAttribute('aria-expanded')).toBe('false')
    // lead's open rows: a, b + its board + d, e + its board + g.
    expect(lead.textContent).toContain('Show 7 items')
    expect(document.querySelectorAll('#oc-cards .oc-card')).toHaveLength(3)
    pick('all').click()
    // the fold is remembered for the group, filter or not.
    const again = document.querySelector<HTMLButtonElement>('button.oc-fold')!
    expect(again.getAttribute('aria-expanded')).toBe('false')
  })

  it('names the choices in Simplified Chinese', () => {
    mount(crew(), 'zh-CN')
    expect(pick('open').textContent).toBe('\u8fdb\u884c\u4e2d3')
    expect(pick('closed').textContent).toBe('\u5df2\u5173\u95ed6')
    expect(pick('all').textContent).toBe('\u5168\u90e8' + '9')
    expect(document.getElementById('oc-filter')!.getAttribute('aria-label')).toBe(
      '\u6309\u72b6\u6001\u7b5b\u9009\u4efb\u52a1',
    )
  })

  it('hides the control when there is no board to filter', () => {
    mount([])
    expect(document.getElementById('oc-filter')!.hidden).toBe(true)
  })

  it('does not store the choice anywhere', () => {
    const { script } = halves()
    expect(script).not.toMatch(/localStorage|sessionStorage|document\.cookie|indexedDB/)
  })
})
