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
    item({ item_id: 'c', title: 'asking', status: 'question' }),
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
    const asking = item({ item_id: 'a', title: 'asking', status: 'question' })
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
    expect(document.getElementById('done')?.textContent).toBe('1 of 2 accepted0 in progress \u00b7 1 need you')
  })

  it('changes nothing on a repeated push of the same values', () => {
    mount(BASE)
    expect(mutations(() => refill(BASE))).toEqual([])
    expect(mutations(() => refill(BASE))).toEqual([])
  })

  it('rebuilds only the cards whose item changed, without a fresh pop-in', () => {
    mount(BASE)
    const [a, b, c] = all('#items .item')
    refill({ ...BASE, items: [BASE.items[0], item({ item_id: 'b', title: 'running', summary: 'half way' }), BASE.items[2]] })
    const after = all('#items .item')
    expect(after[0]).toBe(a)
    expect(after[1]).not.toBe(b)
    expect(after[1].textContent).toContain('half way')
    expect(after[1].classList.contains('pop')).toBe(false)
    expect(after[2]).toBe(c)
    // The band lists only the asking item, which did not move.
    expect(mutations(() => undefined)).toEqual([])
  })

  it('handles several items moving at once, a new one, and a removed one', () => {
    mount(BASE)
    const [a] = all('#items .item')
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
    expect(after[0]).toBe(a)
    expect(after.map((n) => n.querySelector('.t')?.textContent)).toEqual(['first', 'running', 'new work'])
    expect(after[2].classList.contains('pop')).toBe(true)
    // The asking item left, so the band is redrawn as calm.
    expect(document.querySelector('#you .calm')).not.toBeNull()
    expect(document.querySelector('[data-dashboard-field="items"]')?.textContent).toBe('3')
  })

  it('keeps stable cards in place when the order changes', () => {
    mount(BASE)
    const [a, b, c] = all('#items .item')
    refill({ ...BASE, items: [BASE.items[2], BASE.items[0], BASE.items[1]] })
    expect(all('#items .item')).toEqual([c, a, b])
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
