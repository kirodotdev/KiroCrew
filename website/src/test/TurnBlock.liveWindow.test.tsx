/**
 * One fold, both phases. A running turn keeps its newest 5 tool rows open and
 * folds the older ones; a settled turn folds by its mode. The same disclosure
 * value drives both, and a live row (running, awaiting approval, hosting an
 * app, opened by the user, focused) never folds. Every row is one plain keyed
 * <div>, folded with `hidden` on that same element.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createElement, useCallback, useState, type ReactNode } from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import TurnBlock from '../pages/chat/TurnBlock'
import SearchHighlightContext from '../hooks/SearchHighlightContext'
import { useReportRowActive } from '../pages/chat/rowActivity'

const tool = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 Running: step${n}`, ts: `t${n}` }, idx: n })
const hidden = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `✅ step${n}`, ts: `h${n}` }, idx: 200 + n })
const think = (content: string, ts: string, idx: number): TurnItem => ({ kind: 'single', msg: { role: 'thinking', content, ts }, idx })
const say = (content: string, ts: string, idx: number): TurnItem => ({ kind: 'single', msg: { role: 'assistant', content, ts }, idx })
const done = say('Done.', 'z', 50)
const turnOf = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
const tools = (n: number) => Array.from({ length: n }, (_, i) => tool(i))
const tsOf = (it: TurnItem) => (it.kind === 'single' ? String(it.msg.ts) : '')

/** A row body that reports itself live, the way a running or pending pill does. */
function Live({ active, children }: { active: boolean; children: ReactNode }) {
  useReportRowActive(active)
  return <>{children}</>
}
const itemsBody = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}><button>{it.kind === 'single' ? it.msg.content : 'group'}</button></div>
)
/** renderItem whose rows with a `ts` in `live` report active. */
const renderLive = (live: ReadonlySet<string> = new Set()) =>
  (it: TurnItem, i: number) => <Live active={live.has(tsOf(it))}>{itemsBody(it, i)}</Live>
const renderItem = renderLive()

const row = (i: number) => screen.getByTestId(`item-${i}`).closest<HTMLElement>('[data-fold-row]')!
const isFolded = (i: number) => row(i).hasAttribute('inert') && row(i).getAttribute('data-collapsed') === 'true'
const toggle = () => screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded'))!

describe('running window', () => {
  it('7 tool rows: 5 open, 2 behind one fold row', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    expect(toggle()).toHaveAccessibleName('2 earlier steps')
    expect(toggle()).toHaveAttribute('aria-expanded', 'false')
    expect([0, 1].every(isFolded)).toBe(true)
    expect([2, 3, 4, 5, 6].some(isFolded)).toBe(false)
  })

  it('5 tool rows or fewer: no fold row', () => {
    render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    expect(screen.queryAllByRole('button').some(b => b.hasAttribute('aria-expanded'))).toBe(false)
  })

  it('a hidden completion row is neither folded nor counted, and the toggle sits on a real row', () => {
    const items = [tool(0), hidden(0), ...tools(7).slice(1)]
    const { container } = render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(toggle()).toHaveAccessibleName('2 earlier steps')
    expect(isFolded(0)).toBe(true)
    expect(isFolded(1)).toBe(false)
    expect(isFolded(2)).toBe(true)
    // The toggle is right above row 0, the first row it folds.
    expect(container.firstElementChild?.querySelector('button[aria-expanded]')).toBe(toggle())
  })

  it('the reasoning row and answer text never fold', () => {
    const items = [think('plan', 'th', 99), say('Here is what I found so far.', 'a', 100), ...tools(7), think('more', 'th2', 101)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(false)
    expect(isFolded(1)).toBe(false)
    expect([2, 3].every(isFolded)).toBe(true)
  })

  it('a row is a plain <div> that folds by a CSS grid track, never a motion component', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    for (const i of [0, 6]) {
      expect(row(i).tagName).toBe('DIV')
      expect(row(i).getAttribute('style')).toBeNull()
    }
    expect(row(0).className).toContain('grid-rows-[0fr]')
    expect(row(6).className).toContain('grid-rows-[1fr]')
  })

  it('the fold eases height and opacity, and reduced motion drops the transition', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    const cls = row(0).className.split(/\s+/)
    expect(cls.some(c => c.startsWith('transition-') && c.includes('grid-template-rows') && c.includes('opacity'))).toBe(true)
    expect(cls).toContain('motion-reduce:transition-none')
    expect(toggle().querySelector('svg')?.getAttribute('class')).toContain('motion-reduce:transition-none')
  })

  it('only a fold candidate clips its content', () => {
    const items = [say('Here is what I found so far.', 'a', 100), ...tools(7)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(row(0).firstElementChild!.className).not.toContain('overflow-hidden')
    expect(row(1).firstElementChild!.className).toContain('overflow-hidden')
  })
})

describe('live rows never fold', () => {
  it('a running or pending row older than the window stays open and is not counted', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive(new Set(['t0']))} />)
    expect(isFolded(0)).toBe(false)
    expect(isFolded(1)).toBe(true)
    // An older live row above the toggle: 'earlier' would be false, so the count is neutral.
    expect(toggle()).toHaveAccessibleName('1 tool call')
  })

  it('keeps "earlier" when the only live row is newer than the fold', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive(new Set(['t6']))} />)
    expect(toggle()).toHaveAccessibleName('2 earlier steps')
  })

  it('a live row among the newest five does not shift an older row into view', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive(new Set(['t6']))} />)
    expect([0, 1].every(isFolded)).toBe(true)
    expect(isFolded(2)).toBe(false)
  })

  it('a row that MOUNTS live is open in the first render, as a pending pill after a reload is', () => {
    const { container } = render(<TurnBlock turn={turnOf(tools(9))} renderItem={renderLive(new Set(['t0']))} />)
    expect(isFolded(0)).toBe(false)
    expect(container.querySelectorAll('[data-collapsed="true"]')).toHaveLength(3)
  })

  it('a row goes live after folding and comes back; it folds again once idle', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive()} />)
    expect(isFolded(0)).toBe(true)
    rerender(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive(new Set(['t0']))} />)
    expect(isFolded(0)).toBe(false)
    rerender(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLive()} />)
    expect(isFolded(0)).toBe(true)
  })

  it('a row live at completion stays open in the settled turn, even under an explicit fold', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(3))} renderItem={renderLive(new Set(['t1']))} />)
    rerender(<TurnBlock turn={turnOf([...tools(3), done], true)} renderItem={renderLive(new Set(['t1']))} />)
    expect(toggle()).toHaveAccessibleName('2 tool calls')
    expect(screen.queryByTestId('item-0')).toBeNull()
    expect(isFolded(1)).toBe(false)
    fireEvent.click(toggle())
    fireEvent.click(toggle())
    expect(isFolded(1)).toBe(false)
  })

  it('tabbing from a folded-then-shown row onto the toggle keeps the same toggle focused', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    fireEvent.click(toggle())
    const before = toggle()
    act(() => screen.getByTestId('item-0').querySelector('button')!.focus())
    act(() => before.focus())
    expect(toggle()).toBe(before)
    expect(document.activeElement).toBe(before)
  })

  it('a focused row does not fold until focus leaves it', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    const btn = screen.getByTestId('item-0').querySelector('button')!
    act(() => btn.focus())
    rerender(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(false)
    expect(document.activeElement).toBe(btn)
    expect(isFolded(1)).toBe(true)
    act(() => screen.getByTestId('item-6').querySelector('button')!.focus())
    expect(isFolded(0)).toBe(true)
  })
})

describe('a focusout with no relatedTarget', () => {
  afterEach(() => { vi.restoreAllMocks() })
  const hasFocus = (v: boolean) => vi.spyOn(document, 'hasFocus').mockReturnValue(v)
  /** Running turn: row 0 is focused, then a sixth and seventh row push it out of the live window. */
  const focusKeptRow = () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    const btn = screen.getByTestId('item-0').querySelector('button')!
    act(() => btn.focus())
    rerender(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    expect(isFolded(1)).toBe(true)
    expect(isFolded(0)).toBe(false)
    return btn
  }

  it('a tab or window switch keeps the focus-kept row open, and the return keeps the same row', () => {
    const btn = focusKeptRow()
    const r = row(0)
    const mo = new MutationObserver(() => {})
    mo.observe(r, { attributes: true, attributeFilter: ['data-collapsed', 'inert'] })
    hasFocus(false)
    fireEvent.focusOut(btn, { relatedTarget: null })
    expect(isFolded(0)).toBe(false)
    expect(document.activeElement).toBe(btn)
    hasFocus(true)
    fireEvent.focusIn(btn)
    expect(row(0)).toBe(r)
    expect(r).not.toHaveAttribute('data-collapsed')
    expect(r).not.toHaveAttribute('inert')
    expect(mo.takeRecords()).toEqual([])
    mo.disconnect()
  })

  it('focus moving into an MCP App iframe keeps the row open by liveness, as the shape equals a blank click', () => {
    const app: TurnItem = { kind: 'single', msg: { role: 'tool', content: '🔧 Running: app', ts: 'app' }, idx: 300 }
    const withFrame = (it: TurnItem, i: number) => it === app
      ? <Live active><div data-testid={`item-${i}`}><button>app</button><iframe title="app" /></div></Live>
      : renderItem(it, i)
    render(<TurnBlock turn={turnOf([app, ...tools(7)])} renderItem={withFrame} />)
    const btn = screen.getByTestId('item-0').querySelector('button')!
    act(() => btn.focus())
    hasFocus(true)
    fireEvent.focusOut(btn, { relatedTarget: null })
    expect(isFolded(0)).toBe(false)
    expect(row(0).querySelector('iframe')).not.toBeNull()
  })

  it('a click on blank page space folds the focus-kept row', () => {
    const btn = focusKeptRow()
    hasFocus(true)
    fireEvent.focusOut(btn, { relatedTarget: null })
    expect(isFolded(0)).toBe(true)
  })

  it('removing the focused element folds the focus-kept row', () => {
    const btn = focusKeptRow()
    const parent = btn.parentElement!
    hasFocus(true)
    btn.remove()
    // A detached node's event cannot reach the document listener, so the
    // measured focusout is dispatched from where the node was.
    fireEvent.focusOut(parent, { relatedTarget: null })
    expect(isFolded(0)).toBe(true)
  })
})

describe('focus inside an open fold keeps its toggle', () => {
  /** The toggle is a sibling right above row `first`, outside every row. */
  const expectToggleAbove = (t: HTMLElement, first: number) => {
    expect(toggle()).toBe(t)
    expect(t).toHaveAttribute('aria-expanded', 'true')
    expect(t.closest('[data-fold-row]')).toBeNull()
    expect(row(first).previousElementSibling?.contains(t)).toBe(true)
  }
  /** Open the fold, then record the toggle and the row about to take focus. */
  const openFold = (focusRow: number) => {
    fireEvent.click(toggle())
    return { t: toggle(), r: row(focusRow), btn: screen.getByTestId(`item-${focusRow}`).querySelector('button')! }
  }
  const expectRowStill = (i: number, r: HTMLElement) => {
    expect(row(i)).toBe(r)
    expect(r).not.toHaveAttribute('data-collapsed')
    expect(r).not.toHaveAttribute('inert')
  }

  it('settled default turn, one tool row: keyboard focus on that row keeps the same toggle above it', () => {
    render(<TurnBlock turn={turnOf([tool(0), done], true)} renderItem={renderItem} />)
    const { t, r, btn } = openFold(0)
    act(() => btn.focus())
    expect(document.activeElement).toBe(btn)
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
    act(() => btn.blur())
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
  })

  it('settled default turn, one tool row: a mouse click into that row keeps the same toggle above it', async () => {
    const user = userEvent.setup()
    render(<TurnBlock turn={turnOf([tool(0), done], true)} renderItem={renderItem} />)
    const { t, r, btn } = openFold(0)
    await user.click(btn)
    expect(document.activeElement).toBe(btn)
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
    await user.click(document.body)
    expect(document.activeElement).not.toBe(btn)
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
  })

  it('settled default turn, two tool rows: focus on the first keeps the same toggle above it', () => {
    render(<TurnBlock turn={turnOf([...tools(2), done], true)} renderItem={renderItem} />)
    const { t, r, btn } = openFold(0)
    act(() => btn.focus())
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
    act(() => btn.blur())
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
  })

  it('running turn with one folded row: focus on it keeps the same toggle above it', () => {
    render(<TurnBlock turn={turnOf(tools(6))} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(true)
    const { t, r, btn } = openFold(0)
    act(() => btn.focus())
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
    act(() => btn.blur())
    expectToggleAbove(t, 0)
    expectRowStill(0, r)
  })

  it('a closed fold whose only candidate is focused shows no toggle', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    act(() => screen.getByTestId('item-0').querySelector('button')!.focus())
    rerender(<TurnBlock turn={turnOf(tools(6))} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(false)
    expect(screen.queryAllByRole('button').some(b => b.hasAttribute('aria-expanded'))).toBe(false)
  })
})

/** A host that owns the turn's disclosure, as ChatPage does, and can unmount the row. */
function Host({ turn, mounted, collapseAll = false }: { turn: Extract<DisplayItem, { kind: 'turn' }>; mounted: boolean; collapseAll?: boolean }) {
  const [disclosure, setDisclosure] = useState<Record<string, boolean>>({})
  const setFor = useCallback((key: string, v: boolean) => setDisclosure(p => ({ ...p, [key]: v })), [])
  return mounted
    ? <TurnBlock turn={turn} renderItem={renderItem} collapseAll={collapseAll} disclosure={disclosure.k} disclosureKey="k" onDisclosureChange={setFor} />
    : null
}

describe('one disclosure across running, completion, resume and remount', () => {
  it('opened while running, completed while unmounted, remounted: still open', () => {
    const { rerender } = render(<Host turn={turnOf(tools(7))} mounted />)
    fireEvent.click(toggle())
    rerender(<Host turn={turnOf(tools(7))} mounted={false} />)
    rerender(<Host turn={turnOf([...tools(7), done], true)} mounted={false} />)
    rerender(<Host turn={turnOf([...tools(7), done], true)} mounted />)
    expect(toggle()).toHaveAccessibleName('Hide tool calls')
    expect(tools(7).some((_, i) => isFolded(i))).toBe(false)
  })

  it('a settled turn the user opened stays open when it resumes', () => {
    const { rerender } = render(<Host turn={turnOf([...tools(7), done], true)} mounted />)
    fireEvent.click(toggle())
    rerender(<Host turn={turnOf([...tools(7), done, tool(8)], false)} mounted />)
    expect(toggle()).toHaveAccessibleName('Hide earlier steps')
    expect(tools(7).some((_, i) => isFolded(i))).toBe(false)
  })

  it('opened then closed while running: the turn settles closed', () => {
    const { rerender } = render(<Host turn={turnOf(tools(7))} mounted />)
    fireEvent.click(toggle())
    fireEvent.click(toggle())
    rerender(<Host turn={turnOf([...tools(7), done], true)} mounted />)
    expect(toggle()).toHaveAttribute('aria-expanded', 'false')
    expect(toggle()).toHaveAccessibleName('7 tool calls')
  })

  it('without a host, an opened fold stays open through a running-flag flap and completion', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    rerender(<TurnBlock turn={turnOf(tools(7), true)} renderItem={renderItem} />)
    rerender(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    fireEvent.click(toggle())
    rerender(<TurnBlock turn={turnOf([...tools(8), done], true)} renderItem={renderItem} />)
    expect(toggle()).toHaveAttribute('aria-expanded', 'true')
    expect(toggle()).toHaveAccessibleName('Hide tool calls')
  })

  it('a search match in a folded row opens the fold', () => {
    const search = { term: 'later', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0 }
    const items = [say('A later note that names the plan for the build.', 's0', 0), tool(1), say('The build passed and the change is ready for review.', 's2', 2)]
    render(
      <SearchHighlightContext.Provider value={search}>
        <TurnBlock turn={turnOf(items, true)} renderItem={renderItem} collapseAll />
      </SearchHighlightContext.Provider>,
    )
    expect(toggle()).toHaveAttribute('aria-expanded', 'true')
    expect(isFolded(0)).toBe(false)
  })
})

describe('a closed settled default fold drops what cannot go live', () => {
  it('unmounts folded content, keeps the row element, and mounts it again on open', () => {
    const { container } = render(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} />)
    expect(screen.queryByTestId('item-0')).toBeNull()
    expect(container.querySelectorAll('[data-fold-row]')).toHaveLength(8)
    fireEvent.click(toggle())
    expect(isFolded(0)).toBe(false)
  })

  it('keeps the content of a row the host says may be live, so a user-opened pill survives a remount', () => {
    const opened = (it: TurnItem) => tsOf(it) === 't2'
    render(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderLive(new Set(['t2']))} mayBeLive={opened} />)
    expect(isFolded(2)).toBe(false)
    expect(screen.queryByTestId('item-3')).toBeNull()
  })

  it('keeps the content of an app-bearing row, and drops a row that went idle', () => {
    const appRow: TurnItem = { kind: 'single', msg: { role: 'tool', content: '🔧 Running: app', ts: 'ap', meta: { mcp_app: true } }, idx: 400 }
    const { rerender } = render(<TurnBlock turn={turnOf([appRow, ...tools(7)])} renderItem={renderLive(new Set(['t0']))} />)
    rerender(<TurnBlock turn={turnOf([appRow, ...tools(7), done], true)} renderItem={renderLive()} />)
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
    expect(screen.queryByTestId('item-1')).toBeNull()
  })

  it('a row is a one-column grid whose item may shrink, so wide content cannot widen the transcript', () => {
    render(<TurnBlock turn={turnOf([say('Here is a long answer for the build.', 'a', 100), ...tools(7)])} renderItem={renderItem} />)
    expect(row(0).className).toContain('grid-cols-[minmax(0,1fr)]')
    expect(row(0).firstElementChild!.className).toContain('min-w-0')
  })
})

describe('completion never opens a folded row', () => {
  /** Every row that lost its fold marker during the window the observer saw. */
  const watchUnhide = (root: HTMLElement) => {
    const seen: Element[] = []
    const mo = new MutationObserver(() => {})
    mo.observe(root, { subtree: true, attributes: true, attributeFilter: ['data-collapsed'], attributeOldValue: true })
    return () => {
      for (const r of mo.takeRecords()) if (r.oldValue !== null) seen.push(r.target as Element)
      mo.disconnect()
      return seen
    }
  }

  it('default mode, 40 tool rows: no folded row is shown at the completion flip', () => {
    const { container, rerender } = render(<TurnBlock turn={turnOf(tools(40))} renderItem={renderItem} />)
    const stop = watchUnhide(container)
    rerender(<TurnBlock turn={turnOf([...tools(40), done], true)} renderItem={renderItem} />)
    expect(stop()).toEqual([])
    expect(toggle()).toHaveAccessibleName('40 tool calls')
    expect(container.querySelectorAll('[data-collapsed="true"]')).toHaveLength(40)
  })

  it('collapseAll with no answer: the window rows stay folded', () => {
    const { container, rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} collapseAll />)
    const stop = watchUnhide(container)
    rerender(<TurnBlock turn={turnOf(tools(7), true)} renderItem={renderItem} collapseAll />)
    expect(stop()).toEqual([])
    expect([0, 1].every(isFolded)).toBe(true)
  })

  it('collapseAll ending on a short sign-off: tool rows after the answer stay folded', () => {
    const items = [think('plan', 'th', 99), say('Here is the full report on the build, with every step listed.', 'a', 100), ...tools(7), say('ok', 'b', 101)]
    const { container, rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} collapseAll />)
    expect([2, 3].every(isFolded)).toBe(true)
    const stop = watchUnhide(container)
    rerender(<TurnBlock turn={turnOf(items, true)} renderItem={renderItem} collapseAll />)
    expect(stop()).toEqual([])
    expect([0, 2, 3].every(isFolded)).toBe(true)
    expect(isFolded(1)).toBe(false)
  })
})

describe('row identity: an app iframe is never detached', () => {
  let connects = 0
  let disconnects = 0
  if (!customElements.get('x-app-probe')) {
    customElements.define('x-app-probe', class extends HTMLElement {
      connectedCallback() { connects++ }
      disconnectedCallback() { disconnects++ }
    })
  }
  // beforeEach, not afterEach: the previous test's unmount runs after its own hooks.
  beforeEach(() => { connects = 0; disconnects = 0 })
  const app: TurnItem = { kind: 'single', msg: { role: 'tool', content: '🔧 Running: app', ts: 'app', meta: { tool_call_id: 'tc_app' } }, idx: 300 }
  const appIds = new Set(['tc_app'])
  const withApp = (live: ReadonlySet<string>) => (it: TurnItem, i: number) =>
    it === app ? <div data-testid={`item-${i}`}>{createElement('x-app-probe')}</div> : renderLive(live)(it, i)

  it('running to complete with the app row above the live fold', () => {
    const items = [app, ...tools(7)]
    const { rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={withApp(new Set())} appToolCallIds={appIds} />)
    rerender(<TurnBlock turn={turnOf([...items, done], true)} renderItem={withApp(new Set())} appToolCallIds={appIds} />)
    expect(connects).toBe(1)
    expect(disconnects).toBe(0)
  })

  it('the toggle moving above the app row mid-run does not move the app row', () => {
    const items = [tool(0), app, ...tools(7).slice(1)]
    const { rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={withApp(new Set(['t0']))} appToolCallIds={appIds} />)
    expect(isFolded(0)).toBe(false)
    rerender(<TurnBlock turn={turnOf(items)} renderItem={withApp(new Set())} appToolCallIds={appIds} />)
    expect(isFolded(0)).toBe(true)
    expect(disconnects).toBe(0)
  })

  it('a host with no app set: the app row reports itself live and never folds', () => {
    const live = (it: TurnItem, i: number) =>
      it === app ? <Live active><div data-testid={`item-${i}`}>{createElement('x-app-probe')}</div></Live> : renderItem(it, i)
    const items = [app, ...tools(7)]
    const { rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={live} />)
    expect(isFolded(0)).toBe(false)
    rerender(<TurnBlock turn={turnOf([...items, done], true)} renderItem={live} />)
    expect(isFolded(0)).toBe(false)
    expect(disconnects).toBe(0)
  })
})
