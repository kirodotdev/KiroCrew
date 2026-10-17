// @vitest-environment jsdom
/**
 * The `org-chart` page's fold toggles, driven rather than read.
 *
 * `test/test_dashboard_template_views.py` pins what a regex can pin about this
 * template. What is checked HERE is the behaviour: the page's real script, in a real
 * DOM, against the field bag the host hands it, the way `projectReportPage.test.ts`
 * drives the project report.
 *
 * jsdom lays nothing out and runs no CSS transition, so every case reads the page's
 * own state (label text, aria attributes, the closed class, focus) and never a pixel.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(
  __dirname,
  '../../../src/kiro_crew/dashboard_templates/builtin/org-chart/template.html',
)

const RAW = readFileSync(TEMPLATE, 'utf-8')

/**
 * The page in two halves, split by the DOM rather than by a pattern, as
 * `dashboardTemplatesI18n.test.ts` splits it: the parsed script's text, and the
 * parsed body with the script element removed. A parsed script stays inert, so its
 * text is run by `mount`.
 */
function halves() {
  const doc = new DOMParser().parseFromString(RAW, 'text/html')
  const nodes = Array.from(doc.querySelectorAll('script'))
  const script = nodes.map((node) => node.textContent ?? '').join('\n')
  for (const node of nodes) node.remove()
  return { script, body: doc.body }
}

const NOW = Date.parse('2026-10-06T00:40:00Z')

/** One task row of the `workstreams` fold, with only what a case cares about set. */
function task(id: string) {
  return {
    item_id: id,
    title: `task ${id}`,
    state: 'open',
    status: 'progress',
    summary: '',
    spender: `w-${id}`,
    credits: 1,
    credits_reported: true,
    last_report_at: new Date(NOW - 60_000).toISOString(),
    created_at: new Date(NOW - 3_600_000).toISOString(),
  }
}

/** One board; `parent` names the task whose worker runs it, which makes it a sub-lead's. */
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
 * A lead with three tasks: one plain worker, one sub-lead running two workers, and one
 * sub-lead whose own board has nothing dispatched yet.
 */
function crew() {
  return [
    board('lead', [task('a'), task('b'), task('c')]),
    board('sub-full', [task('d'), task('e')], { board: 'lead', item_id: 'b' }),
    board('sub-empty', [], { board: 'lead', item_id: 'c' }),
  ]
}

/**
 * The listeners each mount's script adds to `window`. Every mount runs the script
 * again, so without removing them a later case's host push would also re-render
 * from an earlier case's page state.
 */
const added: Array<[string, EventListenerOrEventListenerObject]> = []

/** Mount the page with *items* as the fold's boards, and run its script. */
function mount(items: unknown[]) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields: { items, omitted: 0, spenders_omitted: 0, last_entry_at: new Date(NOW).toISOString() },
    agentic: [],
    seq: 1,
    stale: false,
    missing: [],
    written_at: {},
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

/** A host push: the page's own message listener re-renders on the next tick. */
function push() {
  window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew:fields' } }))
  vi.advanceTimersByTime(1)
}

const buttons = () => Array.from(document.querySelectorAll<HTMLButtonElement>('button.oc-fold'))

/** The rows a reader sees in *btn*'s group while it is open: every row not under a folded group. */
function openRows(btn: Element) {
  const group = document.getElementById(btn.getAttribute('aria-controls') ?? '')
  const body = group?.querySelector(':scope > .oc-kids-body')
  if (!body) throw new Error('the fold button controls no group body')
  return Array.from(body.querySelectorAll('.oc-node')).filter((row) => {
    for (let at = row.parentElement; at && at !== body; at = at.parentElement) {
      if (at.classList.contains('oc-kids-closed')) return false
    }
    return true
  }).length
}

/** The number a folded button's label says. */
const shown = (btn: Element) => Number((btn.textContent ?? '').match(/\d+/)?.[0] ?? Number.NaN)

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  for (const [type, listener] of added.splice(0)) window.removeEventListener(type, listener)
  vi.useRealTimers()
})

describe('org-chart: the fold toggles', () => {
  it('carries a script and a body, so the halves above are real', () => {
    const { script, body } = halves()
    expect(script, 'the template carries no script').toContain('function render()')
    expect(body.querySelector('#oc-graph')).not.toBeNull()
    expect(body.querySelector('script')).toBeNull()
  })

  it('counts the nothing-dispatched line of an empty lead as its one item', () => {
    mount([board('idle', [])])
    const [lead] = buttons()
    expect(openRows(lead)).toBe(1)
    lead.click()
    expect(lead.getAttribute('aria-expanded')).toBe('false')
    expect(lead.textContent).toContain('Show 1 item')
  })

  it('says on each folded group how many rows open under it', () => {
    mount(crew())
    const total = buttons().length
    expect(total).toBe(3)
    const said: number[] = []
    for (let i = 0; i < total; i++) {
      // A fresh page per button, so each group is folded on its own.
      mount(crew())
      const btn = buttons()[i]
      const rows = openRows(btn)
      btn.click()
      expect(shown(btn), btn.textContent ?? '').toBe(rows)
      said.push(shown(btn))
    }
    // lead: a, b + its board + d + e, c + its board + nothing dispatched.
    expect(said).toEqual([8, 3, 2])
  })

  it('counts a folded sub-lead as its one chip in the lead label', () => {
    mount(crew())
    const [lead, full, empty] = buttons()
    full.click()
    empty.click()
    expect(openRows(lead)).toBe(3)
    lead.click()
    expect(shown(lead)).toBe(3)
  })

  it('keeps keyboard focus on the fold button across a host push', () => {
    mount(crew())
    const sub = buttons()[1]
    const controls = sub.getAttribute('aria-controls')
    sub.focus()
    sub.click()
    push()
    const now = document.activeElement as HTMLElement
    expect(now, 'the push did not rebuild the button').not.toBe(sub)
    expect(now.classList.contains('oc-fold')).toBe(true)
    expect(now.getAttribute('aria-controls')).toBe(controls)
    expect(now.getAttribute('aria-expanded')).toBe('false')
  })

  it('tells the reader that folding a group leaves the cards below in place', () => {
    mount(crew())
    for (const btn of buttons()) {
      expect(btn.title).toBe('Collapses this group in the graph only. The cards below stay.')
    }
  })
})
