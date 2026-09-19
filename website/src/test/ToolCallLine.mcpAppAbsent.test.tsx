/**
 * A tool row names the MCP App it cannot show.
 *
 * The render payload lives only in `chat.mcpApps`, filled by the live
 * `mcp_app_render` event and never persisted, so a reloaded transcript has the
 * turn's text and no frame. Without a marker in the frame's place, a reader who
 * did not watch the app appear cannot tell a degraded view from a turn that is
 * only prose.
 *
 * The signal is `meta.mcp_app`, written by the backend when the app's
 * single-use render is claimed (`chat_runner.py`, pinned by
 * `test/test_mcp_app_absence_marked.py`). These tests drive the row's two
 * inputs -- that flag, and whether a live payload is present -- across all four
 * combinations, so neither one alone decides what appears.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const SLOT = 'slot-1'
const TC = 'tc_app'
// `mcpAppKey`'s separator. Spelled out as the sibling suite does, so the test
// state is built the way the reducer really keys it.
const APP_KEY = `${SLOT}\u001F${TC}`

const NOTICE = /not viewable here/i

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear() })

/** A replayed tool row: no toolLog entry, persisted meta only. */
function row(meta: Record<string, unknown> = {}): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 create_view',
    cls: 'msg msg-tool',
    meta: { tool_call_id: TC, purpose: 'Draw a diagram', ...meta },
  }
}

function store(msg: ChatMessage, withPayload: boolean) {
  return createTestStore({
    chat: {
      messages: [msg],
      toolLog: [],
      slotRunning: false,
      activeSlot: SLOT,
      mcpApps: withPayload
        ? { [APP_KEY]: { session_key: SLOT, tool_call_id: TC } }
        : {},
    } as unknown as ChatState,
  })
}

describe('ToolCallLine MCP app absence', () => {
  it('names the app it cannot show', () => {
    const msg = row({ mcp_app: true })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: store(msg, false),
    })
    expect(screen.getByText(NOTICE)).toBeTruthy()
  })

  it('says nothing on a row that never produced an app', () => {
    // The counterfactual that makes the notice meaningful: an ordinary tool
    // call has no flag, so every other row in a transcript stays untouched.
    const msg = row()
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: store(msg, false),
    })
    expect(screen.queryByText(NOTICE)).toBeNull()
  })

  it('shows the app rather than the notice while the payload is live', () => {
    const msg = row({ mcp_app: true })
    const { container } = renderWithProviders(
      <ToolCallLine message={msg} running={false} />,
      { store: store(msg, true) },
    )
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByText(NOTICE)).toBeNull()
  })

  it('marks it in the side-panel build too, where no tab survives a reload', () => {
    // App tabs are deliberately dropped from persistence (`usePanelTabs`
    // serializeBucket), so with the panel flag on there is no tab to re-focus
    // and the reopen control would point at nothing.
    const msg = row({ mcp_app: true })
    renderWithProviders(
      <ToolCallLine message={msg} running={false} appInPanel onOpenApp={() => {}} />,
      { store: store(msg, false) },
    )
    expect(screen.getByText(NOTICE)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /side.?panel/i })).toBeNull()
  })

  it('draws once for an auto-approved pair, so one lost app reads as one', () => {
    // An auto-approved call writes two rows under one tool_call_id (the
    // pre-approval pill, then the post-approval one) and the backend flags both,
    // so an ungated notice would claim two lost apps where there is one.
    const first: ChatMessage = {
      role: 'tool',
      content: '🔧 create_view',
      cls: 'msg msg-tool',
      ts: '2026-09-19T04:00:00.000000+00:00',
      meta: { tool_call_id: TC, mcp_app: true },
    }
    const second: ChatMessage = {
      ...first,
      content: '✅ create_view',
      ts: '2026-09-19T04:00:00.000001+00:00',
    }
    const store = createTestStore({
      chat: {
        messages: [first, second],
        toolLog: [],
        slotRunning: false,
        activeSlot: SLOT,
        mcpApps: {},
      } as unknown as ChatState,
    })
    const a = renderWithProviders(<ToolCallLine message={first} running={false} />, { store })
    expect(a.container.textContent).toMatch(NOTICE)
    const b = renderWithProviders(<ToolCallLine message={second} running={false} />, { store })
    // The second row of the pair stays bare; the flag alone does not draw it.
    expect(b.container.textContent).not.toMatch(NOTICE)
  })

  it('names the app when the row knows which server it came from', () => {
    // The identity is persisted in the row's own meta, so it survives the reload
    // this notice exists for. Two lost apps then read as two, by name.
    const msg: ChatMessage = {
      role: 'tool',
      content: '✅ create_view',
      cls: 'msg msg-tool',
      ts: '2026-09-19T05:00:00.000000+00:00',
      meta: { tool_call_id: TC, mcp_app: true, mcp_server: 'excalidraw' },
    }
    const store = createTestStore({
      chat: {
        messages: [msg],
        toolLog: [],
        slotRunning: false,
        activeSlot: SLOT,
        mcpApps: {},
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    expect(container.textContent).toMatch(/excalidraw/)
    expect(container.textContent).toMatch(/not viewable here/)
  })

  it('stays generic when the backend supplied no server identity', () => {
    // `mcp_server` is omitted rather than sent empty when unknown, so the
    // unnamed string has to keep covering that row.
    const msg: ChatMessage = {
      role: 'tool',
      content: '✅ create_view',
      cls: 'msg msg-tool',
      ts: '2026-09-19T05:00:01.000000+00:00',
      meta: { tool_call_id: TC, mcp_app: true },
    }
    const store = createTestStore({
      chat: {
        messages: [msg],
        toolLog: [],
        slotRunning: false,
        activeSlot: SLOT,
        mcpApps: {},
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    // Asserted on the generic string's OWN opening, not just the shared tail:
    // interpolating an empty server yields "The  app from this step...", which
    // still carries the tail, so a tail-only assertion cannot tell the two
    // strings apart and passes even when the named one is always used.
    expect(container.textContent).toMatch(/An app from this step is not viewable here/)
    expect(container.textContent).not.toMatch(/\{\{server\}\}/)
    expect(container.textContent).not.toMatch(/ {2}app/)
  })

  it('is plain content, because there is nothing to click', () => {
    const msg = row({ mcp_app: true })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: store(msg, false),
    })
    expect(screen.queryByRole('button', { name: NOTICE })).toBeNull()
  })
})
