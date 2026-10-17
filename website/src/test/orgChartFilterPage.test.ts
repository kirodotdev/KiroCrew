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

  it('keeps the numbers beside the rows honest under a filter', () => {
    mount(crew())
    pick('open').click()
    const g = graphText()
    // lead: 4 dispatched; a, b (context) and e (context) are drawn, but only a matches,
    // so under a filter the first fact reads "1 of 4 shown".
    expect(g).toContain('1 of 4 shown')
    expect(g).not.toContain('3 of 4 shown')
    // the board footer counts the boards drawn: 1 lead + 2 sub-leads.
    expect(document.querySelector('[data-dashboard-field="items"]')!.textContent).toBe('3')
    pick('all').click()
    // under All the plain "dispatched" fact stands and nothing reads "shown".
    expect(graphText()).toContain('4 dispatched')
    expect(graphText()).not.toContain('shown')
    expect(document.querySelector('[data-dashboard-field="items"]')!.textContent).toBe('5')
  })

  it('says in words, with a dashed chip, which kept rows are there only for context', () => {
    mount(crew())
    pick('open').click()
    const ctx = Array.from(document.querySelectorAll('#oc-graph .oc-chip.oc-ctx'))
    // b and e do not match Open themselves; a, d and g do.
    expect(ctx).toHaveLength(2)
    for (const c of ctx) expect(c.textContent).toContain('kept for the tasks under it')
    pick('all').click()
    expect(document.querySelectorAll('#oc-graph .oc-ctx')).toHaveLength(0)
    expect(graphText()).not.toContain('kept for the tasks under it')
  })

  it('puts the context note first on a kept row, so a narrow width cuts the end and not the note', () => {
    mount(crew())
    pick('open').click()
    for (const c of Array.from(document.querySelectorAll('#oc-graph .oc-chip.oc-ctx'))) {
      const facts = c.querySelector('.oc-name span')!.textContent ?? ''
      expect(facts.startsWith('kept for the tasks under it')).toBe(true)
    }
  })

  it('says in the header that its counts follow the filter', () => {
    mount(crew())
    const scope = () => document.getElementById('oc-scope')!.textContent ?? ''
    expect(scope()).not.toContain('filtered')
    pick('closed').click()
    expect(scope()).toContain('filtered')
    pick('all').click()
    expect(scope()).not.toContain('filtered')
  })

  it('puts a visible word before the pills, so the control says what it filters', () => {
    mount(crew())
    expect(document.querySelector('.oc-flabel')!.textContent).toBe('Tasks:')
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

  it('keeps the cards title while cards are drawn, even with no graph root', () => {
    // `stray` names a parent task that does not exist, so it is not drawn in the graph,
    // yet its open task still gets a card.
    mount([
      board('p', [task('x', 'accepted')]),
      board('stray', [task('y')], { board: 'p', item_id: 'missing' }),
    ])
    pick('open').click()
    expect(document.querySelectorAll('#oc-graph .oc-chip')).toHaveLength(0)
    expect(document.querySelectorAll('#oc-cards .oc-card')).toHaveLength(1)
    // the empty line must not claim "no open task" above a card for an open task.
    expect(document.querySelectorAll('#oc-graph .oc-empty')).toHaveLength(0)
    expect(graphText()).not.toContain('No open task to show.')
    expect(document.getElementById('oc-cards-t')!.hidden).toBe(false)
    // with nothing in the graph and no card, the title goes.
    mount([board('p', [task('x', 'accepted')])])
    pick('open').click()
    expect(document.querySelectorAll('#oc-cards .oc-card')).toHaveLength(0)
    expect(graphText()).toContain('No open task to show.')
    expect(document.getElementById('oc-cards-t')!.hidden).toBe(true)
  })

  it('lets a long row shrink inside a folded group, so its Hide button stays in reach', () => {
    // jsdom lays nothing out, so the rule itself is what can be checked here; the
    // 320 px render is in the PR screenshots.
    const css = Array.from(new DOMParser().parseFromString(RAW, 'text/html').querySelectorAll('style'))
      .map((s) => s.textContent)
      .join('\n')
    expect(css).toMatch(/\.oc-kids\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
  })

  it('lets the fact line wrap under 520 px so the counts are not cut behind the ellipsis', () => {
    // jsdom lays nothing out, so the rule itself is what can be checked here; the
    // 320 px render is in the PR screenshots. Inside the narrow media query the fact
    // line (.oc-name span) stops truncating so every count stays on screen.
    const css = Array.from(new DOMParser().parseFromString(RAW, 'text/html').querySelectorAll('style'))
      .map((s) => s.textContent)
      .join('\n')
    const narrow = css.match(/@media\s*\(max-width:\s*520px\)\s*\{([\s\S]*?\n  \})/)
    expect(narrow).not.toBeNull()
    expect(narrow![1]).toMatch(/\.oc-name span\s*\{[^}]*white-space:\s*normal/)
  })

  it('reads the shown count against its total under a filter, in Simplified Chinese too', () => {
    mount(crew(), 'zh-CN')
    pick('open').click()
    // lead board: 4 dispatched, 1 open row drawn -> "4 中显示 1 个".
    expect(graphText()).toContain('4 \u4e2d\u663e\u793a 1 \u4e2a')
    // the context note no longer reuses the "shown" word that the count also carries.
    for (const c of Array.from(document.querySelectorAll('#oc-graph .oc-chip.oc-ctx'))) {
      expect(c.textContent).toContain('\u56e0\u5176\u4e0b\u7684\u4efb\u52a1\u800c\u4fdd\u7559')
    }
  })

  it('does not store the choice anywhere', () => {
    const { script } = halves()
    expect(script).not.toMatch(/localStorage|sessionStorage|document\.cookie|indexedDB/)
  })
})
