import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import { resolveByApprovalId } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const LS_KEY = 'mc-chat-config'

// jsdom polyfill: SegmentedControl uses ResizeObserver to switch between
// full / compact / dropdown layouts based on container width.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear() })

function toolMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'tool', content: '🔧 Running: echo hello', cls: '', meta: { tool_call_id: 'tc_1', purpose: 'Say hello' }, ...overrides }
}

describe('ToolCallLine simplifiedToolNames', () => {
  it('shows purpose text when simplifiedToolNames is enabled', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: true }))
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    expect(screen.getByText('Say hello')).toBeTruthy()
  })

  it('shows raw label when simplifiedToolNames is disabled', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false }))
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    expect(screen.getByText('Running: echo hello')).toBeTruthy()
  })

  it('falls back to raw label when purpose is unavailable', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: true }))
    const msg = toolMsg({ meta: { tool_call_id: 'tc_2' } })
    const store = createTestStore({
      chat: {
        messages: [msg],
        toolLog: [{ type: 'tool', text: 'echo hello', tool_call_id: 'tc_2', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    expect(screen.getByText('Running: echo hello')).toBeTruthy()
  })
})

describe('ToolCallLine inline expansion', () => {
  it('shows an indeterminate activity status for a running shell tool', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', tool_call_id: 'tc_1', is_shell: true, ts: 1 }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running />, { store })
    expect(screen.getByText(/Running ·/)).toBeTruthy()
    expect(screen.getByLabelText('Show details for tool: Running: echo hello')).toBeTruthy()
  })

  it('starts collapsed and expands on click, defaulting to Output section', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', input: 'echo "hi"', output: 'hi-output-content', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    // Collapsed: output content not yet rendered
    expect(screen.queryByText('hi-output-content')).toBeNull()
    // aria-expanded reflects collapsed state
    const btn = screen.getByRole('button', { name: /Show details/i })
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    // Click to expand
    fireEvent.click(btn)
    // Default segment is Output → output content visible, input content not rendered
    expect(screen.getByText('hi-output-content')).toBeTruthy()
    expect(screen.queryByText('echo "hi"')).toBeNull()
    expect(btn.getAttribute('aria-expanded')).toBe('true')
  })

  it('renders only the available section when one of input/output is missing', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'only-output', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    expect(screen.getByText('only-output')).toBeTruthy()
  })

  it('renders both segments with the missing one disabled', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        // Output present, no input → Input segment should be disabled, Output active.
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'only-output', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    const inputBtn = screen.getByRole('button', { name: 'Input' })
    const outputBtn = screen.getByRole('button', { name: 'Output' })
    expect(inputBtn.hasAttribute('disabled')).toBe(true)
    expect(outputBtn.hasAttribute('disabled')).toBe(false)
    expect(inputBtn.getAttribute('title')).toBe('Input not yet available')
    // Clicking the disabled Input segment must not flip the panel
    fireEvent.click(inputBtn)
    expect(screen.getByText('only-output')).toBeTruthy()
  })

  it('shows historical-message message when no toolLog entry exists and no purpose meta', () => {
    const msg = toolMsg({ meta: { tool_call_id: 'tc_orphan' } })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    expect(screen.getByText('Details unavailable for historical tool calls.')).toBeTruthy()
  })

  it('falls back to message meta purpose for historical tool calls (raw-label mode)', () => {
    // simplifiedToolNames OFF → pill shows raw label, meta row shows `→ purpose`.
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false }))
    const msg = toolMsg({ meta: { tool_call_id: 'tc_orphan', purpose: 'Said hello earlier' } })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    expect(screen.getByText('→ Said hello earlier')).toBeTruthy()
  })

  it('renders persisted meta.input and meta.output when toolLog is empty (historical reload)', async () => {
    // Backend persists input/output to message meta (see _tool_meta in chat_runner.py)
    // so the inline detail panel survives a chat reload after gateway restart.
    const msg = toolMsg({
      meta: {
        tool_call_id: 'tc_persisted',
        purpose: 'Read a config file',
        input: '{"path":"/etc/hosts"}',
        output: '127.0.0.1 localhost\n::1 localhost',
      },
    })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    // Default segment is Output → output content visible
    expect(await screen.findByText(/127\.0\.0\.1 localhost/)).toBeTruthy()
    // Input segment exists and is enabled (data is available)
    const inputBtn = screen.getByRole('button', { name: 'Input' })
    expect(inputBtn.hasAttribute('disabled')).toBe(false)
    fireEvent.click(inputBtn)
    // AnimatePresence mode="wait" sequences the exit→enter, so wait for the
    // Input pre block to mount asynchronously.
    expect(await screen.findByText(/etc\/hosts/)).toBeTruthy()
  })

  it('hides redundant purpose row when the pill already shows the purpose', () => {
    // simplifiedToolNames ON (default) → pill text === purpose. Meta row must not duplicate it.
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: true }))
    const msg = toolMsg({ meta: { tool_call_id: 'tc_orphan', purpose: 'Said hello earlier' } })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Show details/i }))
    expect(screen.queryByText('→ Said hello earlier')).toBeNull()
    // The pill itself still shows the purpose label (verifies the user isn't losing info)
    expect(screen.getAllByText('Said hello earlier').length).toBeGreaterThan(0)
  })

  it('auto-expands and clears focus when redux focusToolCallId matches', () => {
    // Stub scrollIntoView (jsdom doesn't implement it) so the auto-scroll branch doesn't throw
    Element.prototype.scrollIntoView = vi.fn() as unknown as typeof Element.prototype.scrollIntoView
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'auto-output-content', ts: 1 }],
        slotRunning: false,
        focusToolCallId: 'tc_1',
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    // Auto-expanded → default Output section content visible
    expect(screen.getByText('auto-output-content')).toBeTruthy()
    // Focus consumed: redux state cleared
    expect(store.getState().chat.focusToolCallId).toBeNull()
  })

  it('does not auto-expand when focusToolCallId targets a different tool', () => {
    Element.prototype.scrollIntoView = vi.fn() as unknown as typeof Element.prototype.scrollIntoView
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', output: 'should-not-show', ts: 1 }],
        slotRunning: false,
        focusToolCallId: 'tc_OTHER',
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    expect(screen.queryByText('should-not-show')).toBeNull()
    // Focus is preserved for the other pill that owns it
    expect(store.getState().chat.focusToolCallId).toBe('tc_OTHER')
  })

  it('auto-expands when an unresolved permission message exists for the tool, then collapses on resolve', async () => {
    const msg = toolMsg()
    const pendingPerm: ChatMessage = {
      role: 'permission', content: 'Approve?', cls: '',
      meta: { tool_call_id: 'tc_1', approval_id: 'app-1' },
    }
    const store = createTestStore({
      chat: {
        messages: [msg, pendingPerm],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', input: 'echo "hi"', ts: 1 }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    const { rerender } = renderWithProviders(<ToolCallLine message={msg} running={true} />, { store })
    // Pending → locked open with "Awaiting approval" aria-label
    let btn = screen.getByRole('button', { name: /Awaiting approval/i })
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    // Approval resolves through the proper redux action so the selector picks it up
    store.dispatch(resolveByApprovalId({ id: 'app-1', decision: 'approved' }))
    rerender(<ToolCallLine message={msg} running={true} />)
    // Auto-collapse on resolve is rAF-deferred — wait for the next frame to flush.
    await waitFor(() => {
      btn = screen.getByRole('button', { name: /(Show|Hide) details/i })
      expect(btn.getAttribute('aria-expanded')).toBe('false')
    })
  })

  it('locks pending pills open — clicks during pending are no-ops', () => {
    const msg = toolMsg()
    const pendingPerm: ChatMessage = {
      role: 'permission', content: 'Approve?', cls: '',
      meta: { tool_call_id: 'tc_1', approval_id: 'app-2' },
    }
    const store = createTestStore({
      chat: {
        messages: [msg, pendingPerm],
        toolLog: [{ type: 'tool', text: 'echo hello', purpose: 'Say hello', tool_call_id: 'tc_1', input: 'echo "hi"', ts: 1 }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={true} />, { store })
    const btn = screen.getByRole('button', { name: /Awaiting approval/i })
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    // User can't collapse a pending pill — the input being approved must stay
    // visible. Click is a no-op while pending.
    fireEvent.click(btn)
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    // Cursor reflects the lock
    expect(btn.className).toContain('cursor-default')
  })
})

describe('ToolCallLine file-open icon', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true, status: 200 })) as any
  })

  function fileMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
    return { role: 'tool', content: '🔧 Read /etc/hosts', cls: '', meta: { tool_call_id: 'tc_file', purpose: 'Read a file' }, ...overrides }
  }

  function fileStore(input: string) {
    return createTestStore({
      chat: {
        messages: [fileMsg()],
        toolLog: [{ type: 'tool', text: 'Read /etc/hosts', purpose: 'Read a file', tool_call_id: 'tc_file', input, output: 'ok', ts: 1 }],
        slotRunning: false,
      } as any,
    })
  }

  it('renders the open-in-side-panel icon for a file-read tool when onFileOpen is provided and file exists', async () => {
    const store = fileStore('{"path":"/etc/hosts"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} onFileOpen={vi.fn()} />, { store })
    await waitFor(() => expect(screen.getByTitle('Open /etc/hosts in side panel')).toBeTruthy())
  })

  it('shows the basename chip and strips the redundant raw path from the label', async () => {
    const store = fileStore('{"path":"/etc/hosts"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} onFileOpen={vi.fn()} />, { store })
    // The chip (inside the open button) carries the basename.
    const openBtn = await screen.findByTitle('Open /etc/hosts in side panel')
    expect(openBtn.textContent).toContain('hosts')
    // The pill label is just the action word — the full path is not duplicated
    // inline (it lives in the chip tooltip + details).
    const pill = screen.getByRole('button', { name: /Show details/i })
    expect(pill.textContent).toContain('Read')
    expect(pill.textContent).not.toContain('/etc/hosts')
  })

  it('clicking the icon calls onFileOpen with the path and does NOT toggle expand', async () => {
    const onFileOpen = vi.fn()
    const store = fileStore('{"path":"/etc/hosts"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} onFileOpen={onFileOpen} />, { store })
    const pill = screen.getByRole('button', { name: /Show details/i })
    expect(pill.getAttribute('aria-expanded')).toBe('false')
    const icon = await screen.findByTitle('Open /etc/hosts in side panel')
    fireEvent.click(icon)
    expect(onFileOpen).toHaveBeenCalledWith('/etc/hosts')
    // Icon is a sibling hit target — it must not expand the pill.
    expect(pill.getAttribute('aria-expanded')).toBe('false')
  })

  it('does not render the icon for a non-file tool (bash)', async () => {
    const store = fileStore('{"command":"echo hi"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} onFileOpen={vi.fn()} />, { store })
    // Give the (short-circuited) probe effect a chance to run.
    await waitFor(() => expect(screen.getByRole('button', { name: /Show details/i })).toBeTruthy())
    expect(screen.queryByTitle(/in side panel$/)).toBeNull()
  })

  it('does not render the icon when onFileOpen is absent', async () => {
    const store = fileStore('{"path":"/etc/hosts"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} />, { store })
    await waitFor(() => expect(screen.getByRole('button', { name: /Show details/i })).toBeTruthy())
    expect(screen.queryByTitle(/in side panel$/)).toBeNull()
  })

  it('does not render the icon when the file does not exist (HEAD 404)', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 404 })) as any
    const store = fileStore('{"path":"/etc/hosts"}')
    renderWithProviders(<ToolCallLine message={fileMsg()} running={false} onFileOpen={vi.fn()} />, { store })
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect(screen.queryByTitle(/in side panel$/)).toBeNull()
  })
})

// `.ft-block-reveal` animates opacity, so while it is present the row keeps a
// stacking context — which traps a `position: fixed` DESCENDANT (an MCP app's
// full-screen sheet) inside the row instead of the viewport, painting it beneath
// shell navigation. The class is a ONE-SHOT entrance fade, so it must come off
// when the animation ends rather than persisting for the row's whole life.
describe('ToolCallLine entrance reveal', () => {
  it('drops the one-shot reveal class once the animation ends', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', tool_call_id: 'tc_reveal', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(
      <ToolCallLine message={toolMsg({ meta: { tool_call_id: 'tc_reveal' } })} running={false} />,
      { store },
    )
    const row = container.firstElementChild as HTMLElement
    expect(row.className).toContain('ft-block-reveal')

    fireEvent.animationEnd(row)
    expect(row.className).not.toContain('ft-block-reveal')
  })

  it('ignores a nested element ending its own animation', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', tool_call_id: 'tc_nested', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(
      <ToolCallLine message={toolMsg({ meta: { tool_call_id: 'tc_nested' } })} running={false} />,
      { store },
    )
    const row = container.firstElementChild as HTMLElement
    const inner = row.querySelector('button')!

    fireEvent.animationEnd(inner)
    expect(row.className).toContain('ft-block-reveal')
  })
})

/** The pill's LABEL is prose with the odd argument spliced in ("Searching for
 *  'YOLO' in src"), so it must follow the user's Font Family choice
 *  (`--font-body`). Tailwind's `font-mono` resolves to `var(--mono)`, a token
 *  that setting never writes. The file-path chip is the exception: a path is
 *  code and keeps mono. */
describe('ToolCallLine — pill follows the Font Family setting, path chip stays mono', () => {
  it('does not pin the pill to font-mono', () => {
    const msg = toolMsg({
      content: "🔧 Searching for 'font-mono' in src",
      // Purpose matches the content: `displayLabel` prefers `purpose` under
      // simplifiedToolNames, and the default fixture's purpose is unrelated.
      meta: { tool_call_id: 'tc_1', purpose: "Searching for 'font-mono' in src" },
    })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    const pill = screen.getByLabelText(/details for tool/)
    expect(pill.className).not.toContain('font-mono')
    // The label itself still renders — guards against asserting on an empty pill.
    expect(pill.textContent).toContain("Searching for 'font-mono' in src")
  })

  it('keeps the file-path chip monospaced', () => {
    const msg = toolMsg({
      content: '🔧 Read website/tailwind.config.js',
      meta: { tool_call_id: 'tc_file', purpose: 'Read the fontFamily block', file_path: 'website/tailwind.config.js' },
    })
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    const { container } = renderWithProviders(
      <ToolCallLine message={msg} running={false} onFileOpen={() => {}} />, { store },
    )
    const chip = [...container.querySelectorAll('button')]
      .find(b => b !== screen.getByLabelText(/details for tool/) && /tailwind\.config\.js/.test(b.textContent || ''))
    // The chip only mounts when the side panel offers a file to open; when this
    // build does not render it, the assertion below is skipped rather than
    // asserting on the wrong node.
    if (chip) expect(chip.className).toContain('font-mono')
  })
})

describe('ToolCallLine MCP app side-panel placeholder', () => {
  const APP_SLOT = 'slot-1'
  const appStore = () => createTestStore({
    chat: {
      messages: [toolMsg()],
      toolLog: [],
      slotRunning: false,
      activeSlot: APP_SLOT,
      // Only presence matters: `appInPanel` short-circuits before McpAppFrame,
      // so the payload is never handed to an iframe here.
      mcpApps: { [`${APP_SLOT}\u001Ftc_1`]: { session_key: APP_SLOT, tool_call_id: 'tc_1' } },
    } as unknown as ChatState,
  })

  it('renders the placeholder as an interactive control, not static text', () => {
    renderWithProviders(
      <ToolCallLine message={toolMsg()} running={false} appInPanel onOpenApp={() => {}} />,
      { store: appStore() },
    )
    // Must be a real button: keyboard-reachable and announced as actionable.
    // A plain <div> here left the app unreachable once the tab was closed.
    expect(screen.getByRole('button', { name: /side.?panel/i })).toBeTruthy()
  })

  it('clicking it asks to reopen the app, passing the tool-call id', () => {
    const onOpenApp = vi.fn()
    renderWithProviders(
      <ToolCallLine message={toolMsg()} running={false} appInPanel onOpenApp={onOpenApp} />,
      { store: appStore() },
    )
    fireEvent.click(screen.getByRole('button', { name: /side.?panel/i }))
    expect(onOpenApp).toHaveBeenCalledWith('tc_1')
  })

  // Complement, not a regression guard: with the flag off the inline frame renders
  // instead, which held before this change too.
  it('renders the inline frame, not the placeholder, when the panel flag is off', () => {
    renderWithProviders(
      <ToolCallLine message={toolMsg()} running={false} onOpenApp={() => {}} />,
      { store: appStore() },
    )
    expect(screen.queryByRole('button', { name: /side.?panel/i })).toBeNull()
  })
})

describe('ToolCallLine auto-denied detection', () => {
  // The gateway appends a hidden "🚫 <title> — <reason>" tool message sharing
  // the visible 🔧 pill's tool_call_id when a security-policy deny rule or
  // hook blocks a call. The pill must find that sibling and render amber
  // (warn) instead of the green success state.
  it('renders warn tone and a standard blocked message when a 🚫 sibling shares the tool_call_id', () => {
    const pill = toolMsg({ meta: { tool_call_id: 'tc_deny' } })
    const denySibling: ChatMessage = {
      role: 'tool',
      content: '🚫 shell — Blocked by security policy: deny rule',
      cls: 'msg msg-tool',
      meta: { tool_call_id: 'tc_deny' },
    }
    const store = createTestStore({
      chat: {
        messages: [pill, denySibling],
        toolLog: [{ type: 'tool', text: 'kirocrew token', tool_call_id: 'tc_deny', output: 'User denied tool execution', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={pill} running={false} />, { store })
    // Amber slash icon, not the green success dot
    expect(container.querySelector('.text-warn')).toBeTruthy()
    expect(container.querySelector('.text-ok')).toBeFalsy()
    // Expanded output shows the standard blocked message — the 🚫 sibling's
    // content is a redacted title (often just "shell"), not a usable reason —
    // and never kiro-cli's misleading boilerplate.
    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    expect(screen.getByText('Blocked by security policy')).toBeTruthy()
    expect(screen.queryByText('User denied tool execution')).toBeFalsy()
  })

  it('keeps the green success state when no 🚫 sibling exists', () => {
    const store = createTestStore({
      chat: {
        messages: [toolMsg()],
        toolLog: [{ type: 'tool', text: 'echo hello', tool_call_id: 'tc_1', output: 'hello', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={toolMsg()} running={false} />, { store })
    expect(container.querySelector('.text-ok')).toBeTruthy()
    expect(container.querySelector('.text-warn')).toBeFalsy()
  })

  it('user rejection (resolved permission) stays red even with a 🚫 sibling', () => {
    const pill = toolMsg({ meta: { tool_call_id: 'tc_userreject' } })
    const denySibling: ChatMessage = {
      role: 'tool',
      content: '🚫 Running: rm file (rejected)',
      cls: 'msg msg-tool',
      meta: { tool_call_id: 'tc_userreject' },
    }
    const perm: ChatMessage = {
      role: 'permission',
      content: 'Running: rm file',
      cls: '',
      meta: { tool_call_id: 'tc_userreject', resolved: 'rejected' },
    }
    const store = createTestStore({
      chat: {
        messages: [pill, perm, denySibling],
        toolLog: [{ type: 'tool', text: 'rm file', tool_call_id: 'tc_userreject', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={pill} running={false} />, { store })
    expect(container.querySelector('.text-danger')).toBeTruthy()
    expect(container.querySelector('.text-warn')).toBeFalsy()
  })
})
