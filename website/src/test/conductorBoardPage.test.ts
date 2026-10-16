// @vitest-environment jsdom
/**
 * The `kirocrew-conductor` page, driven rather than read.
 *
 * The page ships in the Python package and runs in a sandboxed frame, so its script
 * is exercised here the way `projectReportPage.test.ts` drives its own: set
 * `window.kirocrew` as the frame does, insert the markup, run the script.
 *
 * Three sources feed it, and each case checks one: the `work` fold's items, the
 * conductor's agentic `needs_you` lines, and its own `panel` note. Every value is
 * text, so a value that looks like markup must stay text.
 */
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(
  __dirname,
  '../../../src/kiro_crew/dashboard_templates/builtin/kirocrew-conductor/template.html',
)
const RAW = readFileSync(TEMPLATE, 'utf-8')

/** The page split by the DOM: its script text, and its body with the script removed. */
function halves() {
  const doc = new DOMParser().parseFromString(RAW, 'text/html')
  const script = doc.querySelector('script')
  const code = script?.textContent ?? ''
  script?.remove()
  return { code, body: doc.body }
}

const HOSTILE = '<img src=x onerror="window.__pwned=1">'

/** One `work` fold item, with only what a case cares about set. */
function item(over: Record<string, unknown> = {}) {
  return {
    item_id: 'it_1',
    title: 'the task',
    state: 'open',
    status: 'progress',
    summary: '',
    pr: null,
    reason: null,
    ...over,
  }
}

function mount(fields: Record<string, unknown>, agentic: string[] = ['needs_you']) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields,
    agentic,
    seq: 1,
    stale: false,
    missing: [],
    written_at: {},
  }
  const { code, body } = halves()
  document.body.replaceChildren(...Array.from(body.childNodes))
  // The host's own fill: every bound element gets its value by textContent.
  for (const node of Array.from(document.querySelectorAll('[data-dashboard-field]'))) {
    const value = fields[node.getAttribute('data-dashboard-field') ?? '']
    if (value !== undefined) node.textContent = typeof value === 'string' ? value : JSON.stringify(value)
  }
  new Function(code)()
}

const text = (sel: string) => document.querySelector(sel)?.textContent ?? ''
const all = (sel: string) => Array.from(document.querySelectorAll(sel))

describe('kirocrew-conductor page', () => {
  it('draws one card per work item, with its state', () => {
    mount({
      goal: 'ship the board',
      items: [
        item({ item_id: 'a', title: 'accepted one', state: 'accepted', status: 'done' }),
        item({ item_id: 'b', title: 'running one', pr: 42 }),
        item({ item_id: 'c', title: 'claimed one', status: 'done' }),
      ],
    })
    const cards = all('#cb-tasks .card')
    expect(cards.map((c) => c.querySelector('.t')?.textContent)).toEqual([
      'accepted one',
      'running one',
      'claimed one',
    ])
    expect(cards.map((c) => c.querySelector('.pill')?.textContent)).toEqual([
      'accepted',
      'working',
      'conductor checking',
    ])
    expect(cards[1].textContent).toContain('PR #42')
    expect(text('#cb-meter')).toContain('1 of 3 accepted by the conductor')
  })

  it('keeps a title that looks like markup as text', () => {
    mount({ items: [item({ title: HOSTILE, summary: HOSTILE })] })
    expect(document.querySelector('#cb-tasks img')).toBeNull()
    expect(text('#cb-tasks .t')).toBe(HOSTILE)
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('shows the conductor-written needs-you lines, labelled as written', () => {
    mount({ items: [], needs_you: ['Approve the merge of PR 4', HOSTILE] })
    const band = document.querySelector('#cb-you .band')
    expect(band).not.toBeNull()
    expect(all('#cb-you .v').map((n) => n.textContent)).toEqual(['Approve the merge of PR 4', HOSTILE])
    expect(text('#cb-you .tag')).toBe('your mate wrote this')
    expect(text('#cb-you .band')).toContain('Nothing sends until you press send.')
    expect(document.querySelector('#cb-you img')).toBeNull()
  })

  it('draws needs-you in one place only: the note never shows its own needs_you', () => {
    mount({ items: [], note: { next: 'rebase', needs_you: 'Pick the merge order' } })
    expect(text('#cb-note .note')).not.toContain('Pick the merge order')
    expect(text('#cb-note .note')).toContain('Next: rebase')
    expect(text('#cb-you')).toBe('Nothing needs you right now.')
  })

  it('hides the round until one has started', () => {
    mount({ round: 0 })
    expect((document.getElementById('cb-round') as HTMLElement).hidden).toBe(true)
    mount({ round: 2 })
    expect((document.getElementById('cb-round') as HTMLElement).hidden).toBe(false)
  })

  it('does not label a value the host did not mark as written', () => {
    mount({ items: [], needs_you: ['a line'] }, [])
    expect(text('#cb-you .v')).toBe('a line')
    expect(document.querySelector('#cb-you .tag')).toBeNull()
  })

  it('does not list a stuck task again when the conductor wrote a line about it', () => {
    mount({
      items: [item({ title: 'Serve operator pages', status: 'blocked', reason: 'approval' })],
      needs_you: ['Approve the push for Serve operator pages'],
    })
    expect(all('#cb-you .v').map((n) => n.textContent)).toEqual(['Approve the push for Serve operator pages'])
  })

  it('lists a task whose worker asked under needs you, from the record', () => {
    mount({ items: [item({ title: 'stuck one', status: 'blocked', reason: 'approval' })] })
    expect(text('#cb-you .v')).toBe('stuck one \u2014 is stuck (approval)')
  })

  it('shows the published note as the mate wrote it', () => {
    mount({
      items: [],
      note: { next: 'rebase PR 4', needs_you: HOSTILE, tasks: [] },
      note_at: '2026-10-13T02:00:00+00:00',
    })
    const note = document.querySelector('#cb-note .note')
    expect(note).not.toBeNull()
    expect(note?.textContent).toContain('Next: rebase PR 4')
    expect(note?.textContent).not.toContain(HOSTILE)
    // The stamp is shown in the reader's own format, never as the raw string.
    expect(note?.textContent).toContain('sent ')
    expect(note?.textContent).not.toContain('2026-10-13T02:00:00')
    expect(text('#cb-note .tag')).toBe('your mate wrote this')
    expect(document.querySelector('#cb-note img')).toBeNull()
  })

  it('draws no note when nothing was published', () => {
    mount({ items: [], note: {} })
    expect(document.querySelector('#cb-note .note')).toBeNull()
  })

  it('says how many tasks the fold left out, and only when it left some out', () => {
    mount({ items: [item()], omitted: 3 })
    expect((document.getElementById('cb-more') as HTMLElement).hidden).toBe(false)
    expect(text('#cb-more')).toContain('3 more tasks not shown')
    expect(document.querySelector('#cb-more button.reply')).not.toBeNull()
    mount({ items: [item()], omitted: 0 })
    expect((document.getElementById('cb-more') as HTMLElement).hidden).toBe(true)
  })

  it('leaves the dropped log entries out of the meter total, as goal-board does', () => {
    // `omitted` counts fold ENTRIES it dropped, not tasks, so it is no task count.
    mount({ items: [item({ state: 'accepted', status: 'done' }), item({ item_id: 'b', status: 'done' })], omitted: 3 })
    expect(text('#cb-meter')).toContain('1 of 2 accepted by the conductor')
    // The same words as the card's pill, so the two read as one state.
    expect(text('#cb-meter')).toContain('1 conductor checking')
  })

  it('counts the closed tasks the fold forgot in done and total, as goal-board does', () => {
    // A long goal: two tasks still listed, eleven closed ones forgotten by the fold.
    mount({
      items: [item({ state: 'accepted', status: 'done' }), item({ item_id: 'b', status: 'progress' })],
      forgotten: { created: 13, items: 11, accepted: 8, rejected: 1, abandoned: 2, in_round: 0 },
    })
    // done 1 + 8; total 2 + 11, rejected and abandoned included.
    expect(text('#cb-meter')).toContain('9 of 13 accepted by the conductor')
    const bar = document.querySelector('#cb-meter [role="progressbar"]')
    expect(bar?.getAttribute('aria-valuemax')).toBe('13')
    expect(bar?.getAttribute('aria-valuenow')).toBe('9')
  })

  it('keeps a listed rejected task in the total and never calls it working', () => {
    mount({ items: [item({ state: 'accepted', status: 'done' }), item({ item_id: 'b', state: 'rejected' })] })
    expect(text('#cb-meter')).toContain('1 of 2 accepted by the conductor')
    expect(text('#cb-meter')).toContain('0 working')
  })

  it('reads a missing or odd forgotten block as zeros', () => {
    for (const forgotten of [undefined, null, 'x', [], { items: -4, accepted: 'many' }]) {
      mount({ items: [item({ state: 'accepted', status: 'done' }), item({ item_id: 'b' })], forgotten })
      expect(text('#cb-meter')).toContain('1 of 2 accepted by the conductor')
    }
  })

  it('labels the truncation button with what it does', () => {
    mount({ items: [item()], omitted: 3 })
    expect(text('#cb-more button.reply')).toBe('\u270e Ask in chat for all tasks')
  })

  it('puts the tasks that need the reader first, the rest in ledger order', () => {
    mount({
      items: [
        item({ item_id: 'a', title: 'not started', status: null }),
        item({ item_id: 'b', title: 'running', status: 'progress' }),
        item({ item_id: 'c', title: 'stuck', status: 'blocked' }),
      ],
    })
    expect(all('#cb-tasks .card .t').map((n) => n.textContent)).toEqual(['stuck', 'not started', 'running'])
  })

  it('gives a task card that needs the reader the same Reply as the band', () => {
    const sent: Array<{ text: string }> = []
    const spy = vi.spyOn(window.parent, 'postMessage').mockImplementation((m: unknown) => {
      sent.push(m as { text: string })
    })
    mount({
      items: [
        item({ title: 'stuck one', status: 'blocked' }),
        item({ item_id: 'b', title: 'running', status: 'progress' }),
      ],
    })
    const onCards = all('#cb-tasks button.reply') as HTMLButtonElement[]
    expect(onCards).toHaveLength(1)
    onCards[0].click()
    ;(document.querySelector('#cb-you button.reply') as HTMLButtonElement).click()
    expect(sent.map((m) => m.text)).toEqual(['About "stuck one": ', 'About "stuck one": '])
    spy.mockRestore()
  })

  it('offers a Reply button that only fills the chat box', () => {
    const sent: unknown[] = []
    const spy = vi.spyOn(window.parent, 'postMessage').mockImplementation((m: unknown) => {
      sent.push(m)
    })
    mount({ items: [], needs_you: ['Approve the push'] })
    ;(document.querySelector('#cb-you button.reply') as HTMLButtonElement).click()
    expect(sent).toEqual([{ type: 'kirocrew-dashboard:act', text: 'About "Approve the push": ' }])
    spy.mockRestore()
  })

  it('gives every request its own Reply button that names it', () => {
    const sent: Array<{ text: string }> = []
    const spy = vi.spyOn(window.parent, 'postMessage').mockImplementation((m: unknown) => {
      sent.push(m as { text: string })
    })
    mount({ items: [item({ title: 'stuck one', status: 'blocked' })], needs_you: ['first ask'] })
    const buttons = all('#cb-you button.reply') as HTMLButtonElement[]
    expect(buttons).toHaveLength(2)
    for (const b of buttons) b.click()
    expect(sent.map((m) => m.text)).toEqual(['About "first ask": ', 'About "stuck one": '])
    spy.mockRestore()
  })

  it('says there is no goal yet instead of a dash', () => {
    mount({})
    expect(text('h1')).toBe('No goal yet. Give this mate one in chat.')
  })

  it('says so when the board is empty and nothing needs the reader', () => {
    mount({})
    expect(text('#cb-tasks')).toBe('No tasks on this board yet.')
    expect(text('#cb-you')).toBe('Nothing needs you right now.')
  })
})
