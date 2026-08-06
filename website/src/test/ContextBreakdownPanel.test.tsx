/**
 * Context Breakdown panel: the properties that make the chart honest.
 *
 *  - sqrt scaling is the whole reason a 1.5k-char turn stays readable next to a
 *    116k one, so the maths is pinned directly.
 *  - the five every-turn boilerplate blocks must collapse into one bucket, or
 *    the legend drowns in sub-500-char rows.
 *  - the human slice is often a handful of characters; it must keep a floor
 *    width or "your message" silently disappears from every bar.
 *  - the non-KiroCrew remainder is an ESTIMATE (tokens->chars ratio) and must be
 *    marked as one wherever it shows.
 *  - an un-recorded session degrades to a readable empty state, not a crash.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within, fireEvent } from '@testing-library/react'

import {
  ContextBreakdownPanel,
  groupBlocks,
  barWidthPct,
  EVERY_TURN_MEMBERS,
  USER_LABEL,
  type ContextTrace,
  type ContextTurn,
} from '../pages/ContextBreakdownPanel'

afterEach(cleanup)

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  ts: '2026-08-04T00:00:00Z',
  phase: 'per_turn',
  blocks: { request_header: 1576, your_message: 6 },
  total_chars: 1582,
  context_used: 2000,
  context_window: 200000,
  model: 'claude',
  ...over,
})

const trace = (over: Partial<ContextTrace> = {}): ContextTrace => ({
  slot: '578c537a',
  turns: [],
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  estimated_other_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
  ...over,
})

describe('barWidthPct — square-root scale', () => {
  it('compresses the spread the way the mockup does (21,017 of 116,652 -> ~42%)', () => {
    expect(barWidthPct(21017, 116652)).toBeCloseTo(42.45, 1)
  })

  it('gives the widest turn the full width', () => {
    expect(barWidthPct(116652, 116652)).toBe(100)
  })

  it('is wider than a linear scale would be for a small turn', () => {
    // Linear would put 1,582/116,652 at 1.4%; sqrt lifts it to ~11.6% so its
    // composition is still legible — the entire point of the scale choice.
    const linear = (1582 / 116652) * 100
    expect(barWidthPct(1582, 116652)).toBeGreaterThan(linear * 5)
  })

  it('is defensive about zero and empty inputs', () => {
    expect(barWidthPct(0, 116652)).toBe(0)
    expect(barWidthPct(1582, 0)).toBe(0)
  })
})

describe('groupBlocks — every-turn bucket', () => {
  it('collapses exactly the five every-turn members into one label', () => {
    const grouped = groupBlocks({
      surface: 100,
      working_folder: 50,
      request_header: 200,
      reply_format_rules: 30,
      user_display: 20,
      memory: 1000,
      your_message: 6,
    })
    expect(grouped.every_turn).toBe(400)
    expect(grouped.memory).toBe(1000)
    expect(grouped[USER_LABEL]).toBe(6)
    // The members must not survive as their own keys.
    for (const member of EVERY_TURN_MEMBERS) {
      expect(grouped[member]).toBeUndefined()
    }
  })

  it('leaves a trace with no every-turn members untouched', () => {
    expect(groupBlocks({ memory: 10, loaded_skill: 20 })).toEqual({ memory: 10, loaded_skill: 20 })
  })
})

describe('ContextBreakdownPanel rendering', () => {
  it('keeps a six-character human message visible with a minimum width', () => {
    const { container } = render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [turn({ blocks: { request_header: 1576, your_message: 6 }, total_chars: 1582 })],
          totals: { request_header: 1576, your_message: 6 },
          injected_chars: 1582,
          user_chars: 6,
        })}
      />,
    )
    const userSegs = container.querySelectorAll<HTMLElement>('[data-user="true"]')
    expect(userSegs.length).toBeGreaterThan(0)
    // 6 / 1582 = 0.38% would round to an invisible, un-hoverable sliver without
    // the floor. A small floor keeps it visible without the gross over-statement
    // the old label-era value caused on a short bar.
    for (const el of userSegs) expect(el.style.minWidth).toBe('3px')
  })

  it('bakes no text into the bars — colour only, decoded by hover + legend', () => {
    const { container } = render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [turn({ phase: 'session_start', blocks: { agent_instructions: 21221, your_message: 25 }, total_chars: 21246 })],
          totals: { agent_instructions: 21221, your_message: 25 },
          injected_chars: 21246,
          user_chars: 25,
        })}
      />,
    )
    // The bar segments are the coloured divs inside a turn's bar cell. None of
    // them may contain visible text — the user asked for pure colour.
    const cell = container.querySelector('.relative.h-5')
    const bar = cell?.querySelector('div[style*="width"]')
    expect(bar).not.toBeNull()
    for (const seg of Array.from(bar!.children)) {
      expect((seg as HTMLElement).textContent).toBe('')
    }
    // Every segment carries its tooltip text as data, and hovering pops a styled
    // bubble (a real DOM node, so it is filmable — unlike the native title).
    const withTip = Array.from(bar!.children).filter(el => (el as HTMLElement).dataset.tip)
    expect(withTip.length).toBe(bar!.children.length)
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.mouseEnter(bar!.children[0], { clientX: 100, clientY: 100 })
    const tip = screen.getByRole('tooltip')
    expect(tip.textContent).toMatch(/Agent instructions/)
    fireEvent.mouseLeave(bar!.children[0])
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('marks the non-KiroCrew remainder as an estimate', () => {
    const { container } = render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [turn({ blocks: { memory: 4000, your_message: 40 }, total_chars: 4040 })],
          totals: { memory: 4000, your_message: 40 },
          injected_chars: 4040,
          user_chars: 40,
          estimated_other_chars: 6000,
          peak_context_used: 5000,
        })}
      />,
    )
    // The legend entry carries the word, and the hatched segment carries it in
    // its hover tooltip (no baked-in bar text anymore).
    expect(screen.getAllByText(/estimate/i).length).toBeGreaterThan(0)
    const est = container.querySelector('[data-estimate="true"]') as HTMLElement | null
    expect(est).not.toBeNull()
    expect(est!.dataset.tip).toMatch(/estimate/i)
  })

  it('separates the session-start turn from the per-turn group', () => {
    render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [
            turn({ phase: 'session_start', blocks: { memory: 40000, your_message: 200 }, total_chars: 40200 }),
            turn({ phase: 'per_turn', blocks: { loaded_skill: 18000, your_message: 40 }, total_chars: 18040 }),
          ],
          totals: { memory: 40000, loaded_skill: 18000, your_message: 240 },
          injected_chars: 58240,
          user_chars: 240,
          estimated_other_chars: 1000,
          peak_context_used: 12000,
        })}
      />,
    )
    expect(screen.getByText('per turn')).toBeInTheDocument()
    expect(screen.getByText('whole window')).toBeInTheDocument()
    expect(screen.getByText('start')).toBeInTheDocument()
  })

  it('renders a readable empty state for a session with no recorded turns', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [] })} />)
    expect(screen.getByText(/No context breakdown recorded/i)).toBeInTheDocument()
  })

  it('shows a loading state before the first payload', () => {
    const { container } = render(<ContextBreakdownPanel trace={undefined} isLoading />)
    expect(within(container).getByText(/Loading context breakdown/i)).toBeInTheDocument()
  })
})

describe('placement: a per-session tab, not a global page', () => {
  it('is registered as a side-panel view next to Logs', async () => {
    const { PINNED_VIEWS } = await import('../hooks/usePanelTabs')
    const { NEW_MENU_LABEL_KEY, NEW_MENU_DESC_KEY } = await import('../pages/chat/SidePanel')
    // Opened from the + menu like Logs — not auto-pinned, since a session the
    // user never inspects should not carry a permanent extra tab.
    expect(PINNED_VIEWS).not.toContain('context')
    // Both maps are keyed by ViewKind, so a missing entry is a build error; this
    // asserts the pair exists so the menu row can never render label-less.
    expect(NEW_MENU_LABEL_KEY.context).toBeTruthy()
    expect(NEW_MENU_DESC_KEY.context).toBeTruthy()
  })

  it('is hidden from the + menu unless Developer Mode is on', async () => {
    const { newMenuSections } = await import('../pages/chat/SidePanel')
    const kinds = (o: { devMode: boolean; terminalEnabled: boolean }) =>
      newMenuSections(o).flat().map(i => i.kind)
    // Dev mode off: Context breakdown is not offered — it is a developer surface.
    expect(kinds({ devMode: false, terminalEnabled: true })).not.toContain('context')
    // Dev mode on: it appears (right after Logs, closing the diagnostics group).
    const on = kinds({ devMode: true, terminalEnabled: true })
    expect(on).toContain('context')
    expect(on.indexOf('context')).toBe(on.indexOf('logs') + 1)
    // The gate is independent of the Terminal gate, and it now covers Logs as
    // well: both diagnostics views are Developer-Mode-only, so with dev mode off
    // neither is offered no matter what Terminal is doing.
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('logs')
    expect(kinds({ devMode: true, terminalEnabled: false })).toContain('logs')
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('terminal')
  })

  it('carries no session picker — the tab IS the session', () => {
    render(
      <ContextBreakdownPanel
        trace={{
          slot: 'chat-1',
          turns: [turn({ phase: 'session_start', total_chars: 1000, blocks: { memory: 900, your_message: 100 } })],
          totals: { memory: 900, your_message: 100 },
          injected_chars: 1000,
          user_chars: 100,
          estimated_other_chars: 0,
          peak_context_used: 0,
          context_window: 1_000_000,
          window_days: 14,
        } as ContextTrace}
        isLoading={false}
      />,
    )
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})
