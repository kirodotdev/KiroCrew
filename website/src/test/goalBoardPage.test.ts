// @vitest-environment jsdom
/**
 * The `goal-board` page, driven rather than read: a refill must touch only the
 * blocks whose values moved, so the rest keep their nodes and their motion.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(
  __dirname,
  '../../../src/kiro_crew/dashboard_templates/builtin/goal-board/template.html',
)
const RAW = readFileSync(TEMPLATE, 'utf-8')

function halves() {
  const doc = new DOMParser().parseFromString(RAW, 'text/html')
  const script = doc.querySelector('script')
  const code = script?.textContent ?? ''
  script?.remove()
  return { code, body: doc.body }
}

function item(over: Record<string, unknown> = {}) {
  return { item_id: 'it_1', title: 'the task', state: 'open', status: 'progress', summary: '', pr: null, reason: null, ...over }
}

function publish(fields: Record<string, unknown>, locale = 'en') {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = { fields, agentic: [], locale, missing: [], written_at: {} }
  // The host fills bindings before telling the page that a refill landed.
  for (const node of Array.from(document.querySelectorAll('[data-dashboard-field]'))) {
    const value = fields[node.getAttribute('data-dashboard-field') ?? '']
    if (value !== undefined) node.textContent = typeof value === 'string' ? value : JSON.stringify(value)
  }
}

function mount(fields: Record<string, unknown>) {
  const { code, body } = halves()
  document.body.replaceChildren(...Array.from(body.childNodes))
  publish(fields)
  new Function(code)()
}

function refill(fields: Record<string, unknown>, locale = 'en') {
  publish(fields, locale)
  // What the composed frame does on a refill: the posted message, then its event.
  window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:data' } }))
  window.dispatchEvent(new Event('kirocrew-dashboard'))
  vi.runOnlyPendingTimers()
}

const all = (sel: string) => Array.from(document.querySelectorAll(sel))
const BLOCKS = ['you', 'done', 'states', 'tally', 'items']

/** The childList mutations `act` causes inside the drawn blocks. */
function mutations(act: () => void): MutationRecord[] {
  const records: MutationRecord[] = []
  const observer = new MutationObserver((r) => records.push(...r))
  for (const id of BLOCKS) observer.observe(document.getElementById(id) as HTMLElement, { childList: true, subtree: true })
  act()
  records.push(...observer.takeRecords())
  observer.disconnect()
  return records
}

const BASE = {
  goal: 'ship the board',
  items: [
    item({ item_id: 'a', title: 'first', state: 'accepted', status: 'done' }),
    item({ item_id: 'b', title: 'running' }),
    item({ item_id: 'c', title: 'asking', status: 'question', reason: 'needs_human' }),
  ],
}

// Each mount runs the page script again; an earlier mount's listeners must not draw
// into this one's body.
const added: [string, EventListenerOrEventListenerObject][] = []
const realAdd = window.addEventListener.bind(window)
window.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject, opts?: unknown) => {
  added.push([type, listener])
  realAdd(type, listener, opts as AddEventListenerOptions)
}) as typeof window.addEventListener
beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout'] }) })
afterEach(() => {
  vi.useRealTimers()
  for (const [type, listener] of added.splice(0)) window.removeEventListener(type, listener)
  document.body.replaceChildren()
})

describe('goal-board page refill', () => {
  it('leaves every block alone when only an unrelated field moved', () => {
    mount(BASE)
    const cards = all('#items .item')
    const band = document.querySelector('#you .band')
    expect(cards).toHaveLength(3)
    expect(band).not.toBeNull()
    expect(mutations(() => refill({ ...BASE, last_entry_at: '2026-10-31T01:00:00Z' }))).toEqual([])
    expect(all('#items .item')).toEqual(cards)
    expect(document.querySelector('#you .band')).toBe(band)
    expect(document.querySelector('[data-dashboard-field="last_entry_at"]')?.textContent).toBe('2026-10-31T01:00:00Z')
  })

  it('keeps the asking band and its focus when an unrelated running item is accepted', () => {
    const asking = item({ item_id: 'a', title: 'asking', status: 'question', reason: 'needs_human' })
    const running = item({ item_id: 'b', title: 'running' })
    mount({ items: [asking, running] })
    const you = document.getElementById('you') as HTMLElement
    const band = you.querySelector('.band')
    const reply = you.querySelector('button.reply') as HTMLButtonElement
    expect(band).not.toBeNull()
    reply.focus()
    expect(document.activeElement).toBe(reply)
    expect(document.getElementById('done')?.textContent).toContain('0 of 2 accepted')

    const changes = mutations(() => refill({ items: [asking, { ...running, state: 'accepted', status: 'done' }] }))

    expect(you.querySelector('.band')).toBe(band)
    expect(changes.filter((r) => r.target === you || you.contains(r.target))).toEqual([])
    expect(document.activeElement).toBe(reply)
    expect(document.getElementById('done')?.textContent).toBe('1 of 2 accepted0 in progress \u00b7 0 waiting \u00b7 0 quiet \u00b7 0 stuck \u00b7 1 need you')
  })

  it('changes nothing on a repeated push of the same values', () => {
    mount(BASE)
    expect(mutations(() => refill(BASE))).toEqual([])
    expect(mutations(() => refill(BASE))).toEqual([])
  })

  it('rebuilds only the cards whose item changed, without a fresh pop-in', () => {
    mount(BASE)
    // Open items first, the accepted one last.
    const [b, c, a] = all('#items .item')
    refill({ ...BASE, items: [BASE.items[0], item({ item_id: 'b', title: 'running', summary: 'half way' }), BASE.items[2]] })
    const after = all('#items .item')
    expect(after[0]).not.toBe(b)
    expect(after[0].textContent).toContain('half way')
    expect(after[0].classList.contains('pop')).toBe(false)
    expect(after[1]).toBe(c)
    expect(after[2]).toBe(a)
    // The band lists only the asking item, which did not move.
    expect(mutations(() => undefined)).toEqual([])
  })

  it('handles several items moving at once, a new one, and a removed one', () => {
    mount(BASE)
    const a = all('#items .item')[2]
    refill({
      ...BASE,
      items: [
        BASE.items[0],
        item({ item_id: 'b', title: 'running', state: 'accepted', status: 'done' }),
        item({ item_id: 'd', title: 'new work' }),
      ],
    })
    const after = all('#items .item')
    expect(after).toHaveLength(3)
    // Open work first, then the accepted items in ledger order.
    expect(after.map((n) => n.querySelector('.t')?.textContent)).toEqual(['new work', 'first', 'running'])
    expect(after[1]).toBe(a)
    expect(after[0].classList.contains('pop')).toBe(true)
    // The asking item left, so the band is redrawn as calm.
    expect(document.querySelector('#you .calm')).not.toBeNull()
    expect(document.querySelector('[data-dashboard-field="items"]')?.textContent).toBe('3')
  })

  it('keeps stable cards in place when the order changes', () => {
    mount(BASE)
    const [b, c, a] = all('#items .item')
    refill({ ...BASE, items: [BASE.items[2], BASE.items[0], BASE.items[1]] })
    // The two open items swap; the accepted one stays below them.
    expect(all('#items .item')).toEqual([c, b, a])
  })

  it('shows the empty line when items go away, and drops it when they return', () => {
    mount(BASE)
    refill({ ...BASE, items: [] })
    expect(all('#items .item')).toHaveLength(0)
    expect(document.getElementById('items')?.textContent).toContain('No item on this board yet.')
    refill(BASE)
    expect(all('#items .item')).toHaveLength(3)
    expect(document.getElementById('items')?.textContent).not.toContain('No item on this board yet.')
  })

  it('redraws every block when the language changes', () => {
    mount(BASE)
    const cards = all('#items .item')
    refill(BASE, 'zh-CN')
    const after = all('#items .item')
    expect(after).toHaveLength(3)
    expect(after[0]).not.toBe(cards[0])
    expect(document.getElementById('items')?.textContent).toContain('\u5df2\u63a5\u53d7')
  })

  it('draws after the host first fill and ignores unrelated messages', () => {
    const { code, body } = halves()
    document.body.replaceChildren(...Array.from(body.childNodes))
    const ready = vi.spyOn(document, 'readyState', 'get').mockReturnValue('loading')
    new Function(code)()
    publish(BASE)
    window.dispatchEvent(new Event('DOMContentLoaded'))
    ready.mockRestore()
    expect(all('#items .item')).toHaveLength(3)
    expect(document.querySelector('[data-dashboard-field="items"]')?.textContent).toBe('3')
    expect(mutations(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'other' } }))
      vi.runOnlyPendingTimers()
    })).toEqual([])
    expect(RAW).not.toContain("window.addEventListener('message'")
  })
})

describe('goal-board item states', () => {
  const pill = (card: Element) => card.querySelector('.chip.pill')?.textContent ?? ''
  const tally = () => document.getElementById('tally')?.textContent ?? ''

  it('draws needs you, working, waiting, stuck and done from the host facts', () => {
    mount({
      items: [
        item({ item_id: 'y', status: 'blocked', reason: 'approval', worker: 'idle', stale: false }),
        item({ item_id: 'w', worker: 'running', stale: false }),
        item({ item_id: 'p', worker: 'idle', stale: false }),
        item({ item_id: 'q', status: 'question', worker: 'idle', stale: false }),
        item({ item_id: 's', worker: 'closed', stale: true }),
        item({ item_id: 'd', state: 'accepted', status: 'done', worker: 'closed', stale: false }),
      ],
    })
    const cards = all('#items .item')
    expect(cards.map(pill)).toEqual(['needs you', 'working', 'waiting', 'waiting on conductor', 'stuck \u00b7 no chat to resume it', 'accepted'])
    // The worker's chip is its last report, so it never contradicts the pill.
    expect(cards[2].textContent).toContain('last report: progress')
    expect(cards[2].textContent).not.toContain('worker working')
    // Only the worker that is mid-turn pulses.
    expect(cards.map((c) => c.classList.contains('live'))).toEqual([false, true, false, false, false, false])
    expect(cards.map((c) => !!c.querySelector('.dot'))).toEqual([false, true, false, false, false, false])
    expect(cards[0].classList.contains('you')).toBe(true)
    expect(tally()).toContain('needs you 1')
    expect(tally()).toContain('working 1')
    expect(tally()).toContain('waiting 2')
    expect(tally()).toContain('stuck 1')
    // "stuck" means only a task with no chat left; a blocked report says blocked.
    expect(document.getElementById('you')?.textContent).toContain('is blocked')
    expect(document.getElementById('you')?.textContent).not.toContain('stuck')
    expect(tally()).toContain('accepted 1')
    expect(document.getElementById('done')?.textContent).toBe(
      '1 of 6 accepted1 in progress \u00b7 2 waiting \u00b7 0 quiet \u00b7 1 stuck \u00b7 1 need you',
    )
  })

  it('reads a stale item whose chat is still open as quiet, and an idle one with a PR as waiting on it', () => {
    mount({
      items: [
        item({ item_id: 'h', worker: 'idle', stale: false, pr: 7 }),
        item({ item_id: 'n', worker: 'idle', stale: true }),
      ],
    })
    expect(all('#items .item').map(pill)).toEqual(['waiting on PR 7', 'quiet \u00b7 nothing is scheduled to wake it'])
    expect(tally()).toContain('waiting 1')
    expect(tally()).toContain('quiet 1')
    expect(tally()).not.toContain('stuck')
    expect(document.getElementById('done')?.textContent).toContain('1 quiet')
  })

  it('reads an idle item whose worker loops on CI as working, without the mid-turn pulse', () => {
    mount({
      items: [
        item({ item_id: 'l', worker: 'idle', stale: false, looping: true, pr: 7 }),
        // A block the conductor owes an answer stays waiting, loop or not.
        item({ item_id: 'b', worker: 'idle', stale: false, looping: true, status: 'blocked' }),
      ],
    })
    // One wording: "working", never "waiting"; the PR keeps its own chip.
    expect(all('#items .item').map(pill)).toEqual(['working', 'waiting on conductor'])
    expect(tally()).toContain('working 1')
    expect(all('#items .item').map((c) => c.classList.contains('live'))).toEqual([false, false])
  })

  it('a looping worker mid-turn keeps the live pulse and its working pill', () => {
    mount({ items: [item({ item_id: 'r', worker: 'running', stale: false, looping: true, pr: 7 })] })
    const [card] = all('#items .item')
    expect(card.classList.contains('live')).toBe(true)
    expect(pill(card)).not.toContain('waiting on PR')
  })

  it('calls an open report stuck when the main chat closed, as the Crew panel does', () => {
    mount({
      items: [
        item({ item_id: 'o', worker: 'idle', stale: false, orphaned: true }),
        item({ item_id: 'b', status: 'blocked', worker: 'idle', stale: true, orphaned: true }),
        item({ item_id: 'c', worker: 'closed', stale: true, orphaned: true }),
        item({ item_id: 'r', worker: 'running', stale: false, orphaned: true }),
        item({ item_id: 'x', status: 'done', worker: 'idle', stale: false, orphaned: true }),
        item({ item_id: 'u', status: null, worker: 'closed', stale: true, orphaned: true }),
      ],
    })
    const unread = 'stuck \u00b7 the main chat closed, reopen it so its reports are read'
    expect(all('#items .item').map(pill)).toEqual([
      unread, unread, 'stuck \u00b7 no chat to resume it', 'working', 'waiting',
      'quiet \u00b7 nothing is scheduled to wake it',
    ])
    expect(tally()).toContain('stuck 3')
  })

  it('leaves an item whose main chat is open as it was', () => {
    mount({
      items: [
        item({ item_id: 'p', worker: 'idle', stale: false, orphaned: false }),
        item({ item_id: 'n', worker: 'idle', stale: true, orphaned: false }),
      ],
    })
    expect(all('#items .item').map(pill)).toEqual(['waiting', 'quiet \u00b7 nothing is scheduled to wake it'])
    expect(tally()).not.toContain('stuck')
  })

  it('never calls an item with no worker report stuck', () => {
    mount({ items: [item({ item_id: 'u', status: null, worker: 'closed', stale: true })] })
    expect(all('#items .item').map(pill)).toEqual(['quiet \u00b7 nothing is scheduled to wake it'])
    expect(tally()).not.toContain('stuck')
  })

  it('keeps a plain question with the conductor, out of the needs-you band', () => {
    mount({ items: [item({ status: 'question', worker: 'idle', stale: false })] })
    expect(document.querySelector('#you .band')).toBeNull()
    expect(document.querySelector('#you .calm')).not.toBeNull()
    mount({ items: [item({ status: 'question', reason: 'needs_human', worker: 'idle', stale: false })] })
    expect(document.querySelector('#you .band')?.textContent).toContain('needs a person')
  })

  it('stops the pulse when an idle worker reported progress and ended its turn', () => {
    mount({ items: [item({ worker: 'running', stale: false })] })
    expect(document.querySelector('#items .item.live')).not.toBeNull()
    refill({ items: [item({ worker: 'idle', stale: false })] })
    expect(document.querySelector('#items .item.live')).toBeNull()
    expect(pill(all('#items .item')[0])).toBe('waiting')
  })

  it("reads the host's awaits_person, so an answered approval ask leaves the needs-you band", () => {
    const asked = { status: 'blocked', reason: 'approval', stale: false }
    mount({ items: [item({ ...asked, worker: 'idle', awaits_person: true })] })
    expect(pill(all('#items .item')[0])).toBe('needs you')
    // The person approved in the worker's chat: the host says it no longer waits.
    refill({ items: [item({ ...asked, worker: 'running', awaits_person: false })] })
    expect(pill(all('#items .item')[0])).toBe('working')
    expect(document.querySelector('#you .band')).toBeNull()
    expect(tally()).not.toContain('needs you')
  })

  it('falls back to the progress rule for a value without the host facts', () => {
    mount({ items: [item(), item({ item_id: 'b', status: 'question' })] })
    const cards = all('#items .item')
    expect(cards.map(pill)).toEqual(['working', 'waiting on conductor'])
    expect(cards[0].classList.contains('live')).toBe(true)
  })
})

describe('goal-board page: a child conductor board nests under its task', () => {
  const task = (over: Record<string, unknown> = {}) => ({ item_id: 't', title: 'task', state: 'open', status: 'progress', summary: '', ...over })
  const own = { id: 'board-1', parent: null, goal: 'ship', total: 2, accepted: 0, tasks: [task({ item_id: 'a' }), task({ item_id: 'b' })] }
  const child = {
    id: 'board-2', parent: { board: 'board-1', item_id: 'b', title: 'docs' }, goal: 'refresh the docs', total: 2, accepted: 1,
    tasks: [task({ item_id: 'c1', title: 'install page', state: 'accepted', status: 'done' }), task({ item_id: 'c2', title: 'cli reference' })],
  }
  const fields = { items: [item({ item_id: 'a', title: 'notes' }), item({ item_id: 'b', title: 'docs' })], workstreams: [child, own] }

  it("shows the child conductor's workers under the item it conducts, and nowhere else", () => {
    mount(fields)
    const [a, b] = all('#items .item')
    expect(a.querySelector('.nest')).toBeNull()
    const nest = b.querySelector('.nest') as HTMLElement
    expect(nest.textContent).toContain('1 of 2 accepted')
    expect(nest.textContent).toContain('refresh the docs')
    // The recorded state, never "working": a sub-board task carries no runtime facts,
    // so a worker that said `progress` and then went idle must not read as running.
    expect(Array.from(nest.querySelectorAll('.row .rt')).map((r) => r.textContent)).toEqual([
      'acceptedreported doneinstall page',
      'openprogresscli reference',
    ])
  })

  it('marks an open sub-task that asked or is blocked as waiting on its conductor', () => {
    const asked = { ...child, tasks: [task({ item_id: 'c3', title: 'links', status: 'question', summary: 'keep anchors?' })] }
    mount({ ...fields, workstreams: [own, asked] })
    const row = document.querySelector('.nest .row') as HTMLElement
    expect(row.querySelector('.rt')?.textContent).toBe('waiting on conductorhas a questionlinks')
    expect(row.textContent).toContain('reported keep anchors?')
  })

  it('rebuilds the parent card when only the child board moved', () => {
    mount(fields)
    const [a, b] = all('#items .item')
    const moved = { ...child, tasks: [child.tasks[0], task({ item_id: 'c2', title: 'cli reference', summary: 'half way' })] }
    refill({ ...fields, workstreams: [moved, own] })
    const after = all('#items .item')
    expect(after[0]).toBe(a)
    expect(after[1]).not.toBe(b)
    expect(after[1].textContent).toContain('half way')
  })

  it('draws nothing nested without the workstreams field, or for a board that loops back', () => {
    mount({ items: fields.items })
    expect(document.querySelector('.nest')).toBeNull()
    document.body.replaceChildren()
    const loop = { ...own, id: 'board-3', parent: { board: 'board-2', item_id: 'c2' }, tasks: [task({ item_id: 'b' })] }
    const back = { ...child, parent: { board: 'board-3', item_id: 'b' } }
    // board-2 under b, board-3 under board-2's c2, and a board that names board-2's id
    // again under board-3: the walk stops at the id it already drew.
    mount({ ...fields, workstreams: [own, child, loop, back] })
    expect(all('.nest')).toHaveLength(2)
  })
})

describe('goal-board page: abandoned items fold into one row between open and accepted', () => {
  const titles = (sel: string) => all(sel).map((n) => n.querySelector('.t')?.textContent)
  const fold = () => document.getElementById('gb-dropped') as HTMLElement
  const toggle = () => document.getElementById('gb-dropped-tg') as HTMLButtonElement
  const panel = () => document.getElementById('gb-dropped-list') as HTMLElement
  const dropped = (id: string, why = 'superseded') => item({ item_id: id, title: id, state: 'abandoned', status: 'progress', decision: why })
  const accepted = (id: string) => item({ item_id: id, title: id, state: 'accepted', status: 'done' })
  const open = (id: string) => item({ item_id: id, title: id })
  /** The order the reader meets: each open card, the fold row, then each accepted card. */
  const reading = () => Array.from(document.querySelectorAll('#gb-live > .item, #gb-dropped:not([hidden]), #gb-closed > .item'))
    .map((n) => (n.id === 'gb-dropped' ? `fold:${toggle().textContent}` : n.querySelector('.t')?.textContent))

  it('shows open items first, one collapsed row with the count, then accepted items', () => {
    mount({ items: [accepted('a1'), dropped('x1'), open('o1'), dropped('x2'), accepted('a2'), open('o2'), dropped('x3')] })
    expect(reading()).toEqual(['o1', 'o2', 'fold:\u25b83 abandoned', 'a1', 'a2'])
    expect(fold().hidden).toBe(false)
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(toggle().getAttribute('aria-controls')).toBe('gb-dropped-list')
    expect(panel().hidden).toBe(true)
    // The totals still count the abandoned items, as before.
    expect(document.getElementById('done')?.textContent).toContain('2 of 7 accepted')
    expect(document.getElementById('tally')?.textContent).toContain('abandoned 3')
    expect(document.querySelector('[data-dashboard-field="items"]')?.textContent).toBe('7')
  })

  it('opens on click to each abandoned task and its reason, and keeps the choice across a refill', () => {
    const fields = { items: [open('o1'), dropped('x1', 'replaced by o1'), dropped('x2', 'out of scope')] }
    mount(fields)
    toggle().click()
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    expect(panel().hidden).toBe(false)
    expect(titles('#gb-dropped-list .item')).toEqual(['x1', 'x2'])
    expect(panel().textContent).toContain('replaced by o1')
    expect(panel().textContent).toContain('out of scope')
    expect(mutations(() => refill(fields))).toEqual([])
    expect(panel().hidden).toBe(false)
    toggle().click()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(panel().hidden).toBe(true)
  })

  it('is a real button, so Enter and Space reach it from the keyboard', () => {
    mount({ items: [dropped('x1')] })
    expect(toggle().tagName).toBe('BUTTON')
    expect(toggle().type).toBe('button')
    expect(toggle().querySelector('.caret')?.getAttribute('aria-hidden')).toBe('true')
  })

  it('draws no row with zero abandoned items, and one for a single item', () => {
    mount({ items: [open('o1'), accepted('a1')] })
    expect(fold().hidden).toBe(true)
    expect(reading()).toEqual(['o1', 'a1'])
    refill({ items: [open('o1'), dropped('x1'), accepted('a1')] })
    expect(fold().hidden).toBe(false)
    expect(toggle().textContent).toBe('\u25b81 abandoned')
    refill({ items: [open('o1'), accepted('a1')] })
    expect(fold().hidden).toBe(true)
    expect(all('#gb-dropped-list .item')).toHaveLength(0)
  })

  it('keeps an open replacement among the live items while the abandoned one it replaces folds', () => {
    mount({ items: [dropped('first try', 'replaced by retry'), open('retry')] })
    expect(titles('#gb-live .item')).toEqual(['retry'])
    expect(titles('#gb-dropped-list .item')).toEqual(['first try'])
  })

  it('moves a card into the fold when its open item is abandoned', () => {
    mount({ items: [open('o1'), open('o2')] })
    const [, o2] = all('#items .item')
    refill({ items: [open('o1'), dropped('o2')] })
    expect(titles('#gb-live .item')).toEqual(['o1'])
    expect(titles('#gb-dropped-list .item')).toEqual(['o2'])
    expect(all('#gb-dropped-list .item')[0]).not.toBe(o2)
  })

  it('names a task that just moved into the closed fold, and clears the note once opened', () => {
    mount({ items: [open('o1'), open('o2')] })
    const note = () => document.getElementById('gb-dropped-new') as HTMLElement
    refill({ items: [open('o1'), dropped('o2')] })
    expect(note().textContent).toBe('just moved here: o2')
    expect(note().getAttribute('aria-live')).toBe('polite')
    expect(fold().classList.contains('pulse')).toBe(true)
    // An item that was abandoned from the start is no news.
    mount({ items: [dropped('x1')] })
    expect(note().textContent).toBe('')
    expect(fold().classList.contains('pulse')).toBe(false)
    mount({ items: [open('o1')] })
    refill({ items: [dropped('o1')] })
    toggle().click()
    expect(note().textContent).toBe('')
    expect(fold().classList.contains('pulse')).toBe(false)
  })

  it('pops a card in again where it lands when it changes section', () => {
    mount({ items: [open('o1'), open('o2')] })
    vi.runOnlyPendingTimers()
    refill({ items: [open('o1'), accepted('o2')] })
    const [landed] = all('#gb-closed .item')
    expect(landed.classList.contains('pop')).toBe(true)
    refill({ items: [open('o1'), item({ item_id: 'o2', title: 'o2', state: 'accepted', status: 'done', summary: 'merged' })] })
    expect(all('#gb-closed .item')[0].classList.contains('pop')).toBe(false)
  })

  it('names the row in the reader language', () => {
    mount({ items: [dropped('x1'), dropped('x2')] })
    refill({ items: [dropped('x1'), dropped('x2')] }, 'zh-CN')
    expect(toggle().textContent).toBe('\u25b8\u5df2\u653e\u5f03 2 \u9879')
  })
})
