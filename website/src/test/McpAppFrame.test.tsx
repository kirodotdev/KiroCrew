import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from './helpers'
import { act } from '@testing-library/react'
import McpAppFrame from '../components/McpAppFrame'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'
import { __resetRevealedForTests } from '../components/mcpAppReveal'
import { useTheme } from '../hooks/useTheme'

function payload(over: Partial<McpAppRenderPayload> = {}): McpAppRenderPayload {
  return {
    session_key: 'slot-1',
    tool_call_id: 'call-1',
    server: 'excalidraw',
    tool: 'create_view',
    html: '<!doctype html><html><head></head><body>app</body></html>',
    csp: null,
    permissions: null,
    spool_id: 'uuid-1',
    ...over,
  }
}

/** Attach a fake contentWindow (with a postMessage spy) to the iframe, so the
 *  AppBridge's `e.source === iframe.contentWindow` check passes deterministically
 *  regardless of how jsdom treats srcDoc + sandbox iframes, and so we can assert
 *  the host→app replies. Returns the spy. */
function stubContentWindow(iframe: HTMLIFrameElement) {
  const fakeWin = { postMessage: vi.fn() }
  Object.defineProperty(iframe, 'contentWindow', { configurable: true, value: fakeWin })
  return fakeWin
}

/** Dispatch a postMessage as if it came from the app iframe. We set `source`
 *  via defineProperty to bypass jsdom's MessageEvent source-type validation. */
function dispatchFromApp(data: unknown, source: unknown) {
  const evt = new MessageEvent('message', { data })
  Object.defineProperty(evt, 'source', { configurable: true, value: source })
  window.dispatchEvent(evt)
}

describe('McpAppFrame', () => {
  // The progressive-reveal "already animated" cache is module-level (it must
  // outlive a remounted transcript frame), so tests have to reset it or the
  // first animating test would suppress every later one sharing a spool id.
  beforeEach(() => {
    __resetRevealedForTests()
  })

  it('renders a sandboxed iframe WITHOUT allow-same-origin', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-forms')
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin')
  })

  it('renders the server/tool header', () => {
    const { getByText } = renderWithProviders(<McpAppFrame payload={payload()} />)
    expect(getByText('excalidraw')).toBeTruthy()
    expect(getByText('create_view')).toBeTruthy()
  })

  it('sets the iframe allow attribute from requested permissions', () => {
    const { container } = renderWithProviders(
      <McpAppFrame payload={payload({ permissions: { clipboardWrite: {} } })} />,
    )
    expect(container.querySelector('iframe')!.getAttribute('allow')).toBe('clipboard-write')
  })

  it('answers ui/initialize with a JSON-RPC result carrying host context', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    dispatchFromApp({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' }, win)

    expect(win.postMessage).toHaveBeenCalledTimes(1)
    const [reply, target] = win.postMessage.mock.calls[0]
    expect(target).toBe('*')
    expect(reply.jsonrpc).toBe('2.0')
    expect(reply.id).toBe(1)
    expect(reply.result.protocolVersion).toBe('2025-11-21')
    expect(reply.result.hostInfo).toEqual({ name: 'kirocrew', version: '0.1' })
    expect(reply.result.hostContext.displayMode).toBe('inline')
    expect(reply.result.hostContext.availableDisplayModes).toEqual(['inline', 'fullscreen'])
    expect(reply.result.hostContext.containerDimensions.maxHeight).toBe(1200)
  })

  it('sends tool-input then tool-result (with structuredContent) after initialized', () => {
    const { container } = renderWithProviders(
      <McpAppFrame payload={payload({ structured_content: { foo: 'bar' } })} />,
    )
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)

    expect(win.postMessage).toHaveBeenCalledTimes(2)
    const methods = win.postMessage.mock.calls.map((c) => c[0].method)
    expect(methods).toEqual(['ui/notifications/tool-input', 'ui/notifications/tool-result'])
    expect(win.postMessage.mock.calls[0][0].params).toEqual({ arguments: {} })
    const toolResult = win.postMessage.mock.calls[1][0]
    expect(toolResult.params.content).toEqual([])
    expect(toolResult.params.structuredContent).toEqual({ foo: 'bar' })
  })

  it('sends a null structuredContent when the payload has none', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)
    dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)
    expect(win.postMessage.mock.calls[1][0].params.structuredContent).toBeNull()
  })

  it('forwards the ORIGINATING tool arguments and result content when present', () => {
    // Apps that initialize from their inputs must get the
    // real tools/call state, not empty placeholders.
    const { container } = renderWithProviders(
      <McpAppFrame
        payload={payload({
          tool_input: { url: 'https://example.com/a.pdf' },
          result_content: [{ type: 'text', text: 'opened' }],
        })}
      />,
    )
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)
    dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)
    expect(win.postMessage.mock.calls[0][0].params).toEqual({
      arguments: { url: 'https://example.com/a.pdf' },
    })
    expect(win.postMessage.mock.calls[1][0].params.content).toEqual([
      { type: 'text', text: 'opened' },
    ])
  })

  // ---- progressive reveal (host-paced tool-input-partial) -------------------

  /** A JSON-string element array, the shape excalidraw's create_view sends. */
  function elementsArg(n: number): string {
    return JSON.stringify(Array.from({ length: n }, (_, i) => ({ id: `e${i}` })))
  }

  it('reveals array arguments as tool-input-partial, then the complete input, then the result', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { container } = renderWithProviders(
        <McpAppFrame
          payload={payload({
            tool_input: { elements: elementsArg(5) },
            result_content: [{ type: 'text', text: 'drawn' }],
          })}
        />,
      )
      const win = stubContentWindow(container.querySelector('iframe')!)

      dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)

      // The first partial is synchronous: the app paints immediately instead of
      // waiting out a step interval before anything appears.
      expect(win.postMessage).toHaveBeenCalledTimes(1)
      expect(win.postMessage.mock.calls[0][0].method).toBe('ui/notifications/tool-input-partial')

      act(() => {
        vi.runAllTimers()
      })

      const methods = win.postMessage.mock.calls.map((c) => c[0].method)
      // Every partial precedes the completion pair, and the result is LAST —
      // an app must never see a result before its arguments are complete.
      expect(methods.slice(-2)).toEqual([
        'ui/notifications/tool-input',
        'ui/notifications/tool-result',
      ])
      expect(new Set(methods.slice(0, -2))).toEqual(new Set(['ui/notifications/tool-input-partial']))
      expect(methods.filter((m) => m === 'ui/notifications/tool-input-partial').length).toBe(3)

      // Partials grow monotonically and stay short of the full array.
      const counts = win.postMessage.mock.calls
        .filter((c) => c[0].method === 'ui/notifications/tool-input-partial')
        .map((c) => (JSON.parse(c[0].params.arguments.elements as string) as unknown[]).length)
      // Prefixes start at 2 because the app drops each frame's last element.
      expect(counts).toEqual([2, 3, 4])

      // The COMPLETE notification carries the whole payload, unmodified.
      const complete = win.postMessage.mock.calls.at(-2)![0]
      expect((JSON.parse(complete.params.arguments.elements as string) as unknown[]).length).toBe(5)
      expect(win.postMessage.mock.calls.at(-1)![0].params.content).toEqual([
        { type: 'text', text: 'drawn' },
      ])
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a duplicate initialized so no partial can land after the result', () => {
    // Without the guard, the second
    // `initialized` sees the spool already marked revealed, takes the no-plan
    // branch and posts complete-input + result immediately, while chain 1's
    // timer keeps firing partials afterwards — a partial-aware app would repaint
    // a truncated prefix over the finished diagram and stay there.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { container } = renderWithProviders(
        <McpAppFrame payload={payload({ tool_input: { elements: elementsArg(8) } })} />,
      )
      const win = stubContentWindow(container.querySelector('iframe')!)
      const init = { jsonrpc: '2.0', method: 'ui/notifications/initialized' }

      dispatchFromApp(init, win)
      dispatchFromApp(init, win) // duplicate, mid-reveal
      act(() => {
        vi.runAllTimers()
      })

      const methods = win.postMessage.mock.calls.map((c) => c[0].method)
      // Exactly one completion pair, and it is the LAST thing posted.
      expect(methods.filter((m) => m === 'ui/notifications/tool-input').length).toBe(1)
      expect(methods.filter((m) => m === 'ui/notifications/tool-result').length).toBe(1)
      expect(methods.slice(-2)).toEqual([
        'ui/notifications/tool-input',
        'ui/notifications/tool-result',
      ])
      // No partial after the complete input.
      const completeAt = methods.indexOf('ui/notifications/tool-input')
      expect(methods.slice(completeAt).includes('ui/notifications/tool-input-partial')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('skips the reveal entirely when the user prefers reduced motion', () => {
    // The stub must be a COMPLETE MediaQueryList: the theme provider subscribes
    // with mql.addEventListener during render, and a bare {matches} object
    // throws there — wedging React's act queue for every later test in the file.
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes('prefers-reduced-motion'),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(() => false),
      })),
    )
    try {
      const { container } = renderWithProviders(
        <McpAppFrame payload={payload({ tool_input: { elements: elementsArg(6) } })} />,
      )
      const win = stubContentWindow(container.querySelector('iframe')!)
      dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)

      expect(win.postMessage.mock.calls.map((c) => c[0].method)).toEqual([
        'ui/notifications/tool-input',
        'ui/notifications/tool-result',
      ])
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('does not replay the reveal when the same spool remounts', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const p = payload({ tool_input: { elements: elementsArg(5) } })
      const first = renderWithProviders(<McpAppFrame payload={p} />)
      const win1 = stubContentWindow(first.container.querySelector('iframe')!)
      dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win1)
      act(() => {
        vi.runAllTimers()
      })
      first.unmount()

      // Same spool id → history must not animate again on a scroll-back remount.
      const second = renderWithProviders(<McpAppFrame payload={p} />)
      const win2 = stubContentWindow(second.container.querySelector('iframe')!)
      dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win2)
      expect(win2.postMessage.mock.calls.map((c) => c[0].method)).toEqual([
        'ui/notifications/tool-input',
        'ui/notifications/tool-result',
      ])
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels an in-flight reveal on unmount instead of posting into a dead frame', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { container, unmount } = renderWithProviders(
        <McpAppFrame payload={payload({ tool_input: { elements: elementsArg(20) } })} />,
      )
      const win = stubContentWindow(container.querySelector('iframe')!)
      dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, win)
      const afterFirstFrame = win.postMessage.mock.calls.length

      unmount()
      act(() => {
        vi.runAllTimers()
      })

      expect(win.postMessage.mock.calls.length).toBe(afterFirstFrame)
    } finally {
      vi.useRealTimers()
    }
  })

  it('relays tools/call to POST /api/mcp-apps/call and posts back the result', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ result: { content: [{ type: 'text', text: 'saved' }] } }),
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      const { container } = renderWithProviders(<McpAppFrame payload={payload({ spool_id: 'a'.repeat(32), callback_secret: 'sekret-cap' })} />)
      const iframe = container.querySelector('iframe')!
      const win = stubContentWindow(iframe)

      dispatchFromApp({ jsonrpc: '2.0', id: 9, method: 'tools/call', params: { name: 'save_state', arguments: { x: 1 } } }, win)
      await vi.waitFor(() => expect(win.postMessage).toHaveBeenCalledTimes(1))

      expect(fetchMock).toHaveBeenCalledWith('/api/mcp-apps/call', expect.objectContaining({ method: 'POST' }))
      const relayCall = fetchMock.mock.calls.find((c) => c[0] === '/api/mcp-apps/call')!
      const sent = JSON.parse((relayCall[1] as { body: string }).body)
      // The callback capability the gateway authorizes on is forwarded,
      // NOT just the model-visible spool_id.
      expect(sent).toEqual({ spool_id: 'a'.repeat(32), callback_secret: 'sekret-cap', tool: 'save_state', arguments: { x: 1 } })
      // Session-ownership binding: the endpoint verifies the caller's session
      // owns the spool record, so the relay must present it.
      const headers = (relayCall[1] as { headers: Record<string, string> }).headers
      expect(headers['X-Session-Key']).toBe('slot-1')
      const reply = win.postMessage.mock.calls[0][0]
      expect(reply.id).toBe(9)
      expect(reply.result).toEqual({ content: [{ type: 'text', text: 'saved' }] })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('maps a rejected tools/call relay to a JSON-RPC error reply', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ error: 'tool not app-visible' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      const win = stubContentWindow(iframe)

      dispatchFromApp({ jsonrpc: '2.0', id: 10, method: 'tools/call', params: { name: 'secret' } }, win)
      await vi.waitFor(() => expect(win.postMessage).toHaveBeenCalledTimes(1))

      const reply = win.postMessage.mock.calls[0][0]
      expect(reply.id).toBe(10)
      expect(reply.error).toEqual({ code: -32000, message: 'tool not app-visible' })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects a tools/call without a tool name with -32602 (no network)', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    try {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      const win = stubContentWindow(iframe)

      dispatchFromApp({ jsonrpc: '2.0', id: 9, method: 'tools/call', params: {} }, win)

      expect(fetchMock.mock.calls.some((c) => c[0] === '/api/mcp-apps/call')).toBe(false)
      expect(win.postMessage).toHaveBeenCalledTimes(1)
      const reply = win.postMessage.mock.calls[0][0]
      expect(reply.id).toBe(9)
      expect(reply.error).toEqual({ code: -32602, message: 'missing tool name' })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects a genuinely unsupported request with JSON-RPC -32601', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    dispatchFromApp({ jsonrpc: '2.0', id: 9, method: 'resources/read', params: {} }, win)

    expect(win.postMessage).toHaveBeenCalledTimes(1)
    const reply = win.postMessage.mock.calls[0][0]
    expect(reply.id).toBe(9)
    expect(reply.error).toEqual({ code: -32601, message: 'not supported yet' })
  })

  it('honors ui/notifications/size-changed by resizing (capped at 1200)', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    act(() => dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 640 } }, win))
    expect(iframe.style.height).toBe('640px')

    act(() => dispatchFromApp({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 99999 } }, win))
    expect(iframe.style.height).toBe('1200px')
  })

  it('ignores messages whose source is not the app iframe', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    // A different window object → must be rejected (no reply).
    dispatchFromApp({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' }, { postMessage: vi.fn() })
    expect(win.postMessage).not.toHaveBeenCalled()
  })

  it('retires the bridge on a navigation-start signal (pre-load window)', async () => {
    // A navigated-to page's <head> script can post tools/call
    // BEFORE the iframe `load` event fires. The bridge-guard bootstrap posts
    // {__kirocrew_nav__:1} on the original document's pagehide/beforeunload
    // (which precede the new document's scripts), so the host must retire the
    // bridge eagerly and ignore every subsequent message.
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    try {
      const { container } = renderWithProviders(
        <McpAppFrame payload={payload({ spool_id: 'a'.repeat(32), callback_secret: 'sekret-cap' })} />,
      )
      const iframe = container.querySelector('iframe')!
      const win = stubContentWindow(iframe)

      // Navigation starts → our bootstrap signals the host from the SAME window.
      dispatchFromApp({ __kirocrew_nav__: 1 }, win)
      // The replacement document (same contentWindow) tries to drive a call.
      dispatchFromApp(
        { jsonrpc: '2.0', id: 42, method: 'tools/call', params: { name: 'exfil', arguments: {} } },
        win,
      )

      expect(fetchMock.mock.calls.some((c) => c[0] === '/api/mcp-apps/call')).toBe(false)
      expect(win.postMessage).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

/**
 * Display-mode negotiation (SEP-1865 `ui/request-display-mode`). Without it the
 * request falls through to the method-not-found default, so an app that gates
 * its INTERACTIVE surface on `fullscreen` (excalidraw only mounts its editable
 * canvas there) can never leave its static preview — the rendered diagram looks
 * inert and the app's own fullscreen button is dead.
 */
describe('McpAppFrame — display mode', () => {
  it('grants an app-requested fullscreen and reports the mode actually set', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    dispatchFromApp(
      { jsonrpc: '2.0', id: 7, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
      win,
    )

    const reply = win.postMessage.mock.calls[0][0]
    expect(reply.id).toBe(7)
    expect(reply.error).toBeUndefined()
    expect(reply.result).toEqual({ mode: 'fullscreen' })
  })

  it('honors fullscreen as an EXPANDED BUBBLE, not a viewport overlay', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    act(() => {
      dispatchFromApp(
        { jsonrpc: '2.0', id: 1, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
        win,
      )
    })

    // The frame grows in place to the granted height (viewport-derived, capped
    // at MAX_HEIGHT) and must NOT become a fixed/overlay element covering the
    // transcript — inline apps stay in the conversation.
    const expected = Math.min(1200, Math.round(window.innerHeight * 0.8))
    expect(iframe.style.height).toBe(`${expected}px`)
    // Not a viewport overlay: no ancestor may be position:fixed. (Asserting the
    // IFRAME's own position is vacuous -- the code never sets it, so that check
    // passed unconditionally and would miss a fixed WRAPPER.)
    for (let el = iframe.parentElement; el; el = el.parentElement) {
      expect(getComputedStyle(el).position).not.toBe('fixed')
    }
  })

  it('reports the live mode (not a hardcoded inline) on a later ui/initialize', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    act(() => {
      dispatchFromApp(
        { jsonrpc: '2.0', id: 1, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
        win,
      )
    })
    dispatchFromApp({ jsonrpc: '2.0', id: 2, method: 'ui/initialize' }, win)

    const init = win.postMessage.mock.calls.at(-1)![0]
    expect(init.result.hostContext.displayMode).toBe('fullscreen')
  })

  it('keeps the mode unchanged for an unsupported mode instead of erroring', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    dispatchFromApp(
      { jsonrpc: '2.0', id: 3, method: 'ui/request-display-mode', params: { mode: 'pip' } },
      win,
    )

    const reply = win.postMessage.mock.calls[0][0]
    expect(reply.error).toBeUndefined()
    expect(reply.result).toEqual({ mode: 'inline' })
  })

  it('notifies the app when the HOST expand button changes the mode', () => {
    const { container, getByLabelText } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    act(() => { getByLabelText('Expand app').click() })

    const note = win.postMessage.mock.calls.at(-1)![0]
    expect(note.method).toBe('ui/notifications/host-context-changed')
    expect(note.id).toBeUndefined() // a notification carries no id
    expect(note.params.displayMode).toBe('fullscreen')
  })

  /**
   * The dimension contract. An app cannot lay out a fullscreen surface from a
   * ceiling alone — inside an iframe `position: fixed` yields no body height, so
   * excalidraw keys its fullscreen layout off `containerDimensions.height` and
   * renders into a zero-height container when only `maxHeight` is sent. The mode
   * flips, the editor mounts, and nothing visibly expands.
   */
  it('grants a CONCRETE height (not just maxHeight) for fullscreen', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    act(() => {
      dispatchFromApp(
        { jsonrpc: '2.0', id: 1, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
        win,
      )
    })

    // The grant is followed by a context update carrying the height.
    const note = win.postMessage.mock.calls
      .map((c) => c[0])
      .find((m) => m.method === 'ui/notifications/host-context-changed')
    expect(note).toBeDefined()
    expect(typeof note.params.containerDimensions.height).toBe('number')
    expect(note.params.containerDimensions.height).toBeGreaterThan(0)
  })

  it('advertises maxHeight (a ceiling) while inline', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    dispatchFromApp({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' }, win)

    const dims = win.postMessage.mock.calls[0][0].result.hostContext.containerDimensions
    expect(dims.maxHeight).toBe(1200)
    expect(dims.height).toBeUndefined()
  })
})

/**
 * ui/open-link. The app's "Open in Excalidraw" button uploads the diagram via
 * tools/call and THEN calls openLink. Without a handler that second call hits the
 * -32601 default, so the export succeeds and the tab never opens — a silent dead
 * end. The URL comes from sandboxed app content, so it is untrusted input.
 */
describe('McpAppFrame — ui/open-link', () => {
  function setup() {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)
    // Real browsers return NULL from window.open whenever noopener/noreferrer is
    // in the feature string (HTML spec). Stubbing a truthy Window here is what
    // let an `isError: !opened` regression pass — so mirror reality.
    const openSpy = vi.fn(() => null)
    vi.stubGlobal('open', openSpy)
    return { win, openSpy }
  }

  const ask = (win: unknown, url: unknown) =>
    dispatchFromApp({ jsonrpc: '2.0', id: 9, method: 'ui/open-link', params: { url } }, win)

  it('opens an https URL with noopener,noreferrer and reports success', () => {
    const { win, openSpy } = setup()
    try {
      ask(win, 'https://excalidraw.com/#json=abc')
      expect(openSpy).toHaveBeenCalledWith(
        'https://excalidraw.com/#json=abc', '_blank', 'noopener,noreferrer',
      )
      // Success must be reported even though window.open returned null.
      expect(win.postMessage.mock.calls[0][0].result).toEqual({ isError: false })
    } finally { vi.unstubAllGlobals() }
  })

  it.each([
    ['javascript:alert(1)', 'script execution in the host origin'],
    ['data:text/html,<script>1</script>', 'attacker-authored document'],
    ['file:///etc/passwd', 'local disk read'],
    ['http://excalidraw.com', 'cleartext'],
    ['/relative/path', 'not absolute'],
    ['', 'empty'],
  ])('refuses %s (%s) without navigating', (url) => {
    const { win, openSpy } = setup()
    try {
      ask(win, url)
      expect(openSpy).not.toHaveBeenCalled()
      // Refusal is reported in-band so the app can surface it.
      expect(win.postMessage.mock.calls[0][0].result).toEqual({ isError: true })
      expect(win.postMessage.mock.calls[0][0].error).toBeUndefined()
    } finally { vi.unstubAllGlobals() }
  })
})

describe('McpAppFrame — declared capabilities', () => {
  it('advertises the capabilities it actually implements', () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    dispatchFromApp({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' }, win)

    const caps = win.postMessage.mock.calls[0][0].result.hostCapabilities
    // serverTools: the tools/call relay. openLinks: the handler above.
    expect(caps.serverTools).toBeDefined()
    expect(caps.openLinks).toBeDefined()
    // NOT advertised — deliberately still unimplemented (no backend route).
    expect(caps.updateModelContext).toBeUndefined()
  })
})

describe('McpAppFrame — resize while expanded', () => {
  it('re-notifies the app so the granted height cannot go stale', async () => {
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const iframe = container.querySelector('iframe')!
    const win = stubContentWindow(iframe)

    act(() => {
      dispatchFromApp(
        { jsonrpc: '2.0', id: 1, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
        win,
      )
    })
    const before = win.postMessage.mock.calls.length

    // Shrink the viewport and fire resize; the debounce is 150ms.
    act(() => {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 400 })
      window.dispatchEvent(new Event('resize'))
    })
    await vi.waitFor(() => expect(win.postMessage.mock.calls.length).toBeGreaterThan(before))

    const note = win.postMessage.mock.calls.at(-1)![0]
    expect(note.method).toBe('ui/notifications/host-context-changed')
    // The advertised height must equal what the frame actually renders, or the
    // app lays out against a stale value and gets clipped. The frame height is
    // committed by React, so converge on it rather than sampling one tick early.
    expect(note.params.containerDimensions.height).toBe(Math.round(400 * 0.8))
    await vi.waitFor(() =>
      expect(iframe.style.height).toBe(`${Math.round(400 * 0.8)}px`),
    )
  })
})

/**
 * App diagnostics. excalidraw routes its whole display-mode / editor-lifecycle
 * trace through app.sendLog -> notifications/message. Dropping it (spec-legal for
 * an unknown notification) is what made a stuck app impossible to debug from
 * outside, so the host forwards it to the console instead.
 */
describe('McpAppFrame — app log forwarding', () => {
  const send = (win: unknown, params: unknown) =>
    dispatchFromApp({ jsonrpc: '2.0', method: 'notifications/message', params }, win)

  it('forwards an app log to the console, tagged with server/tool', () => {
    const spy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    try {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const win = stubContentWindow(container.querySelector('iframe')!)

      send(win, { level: 'info', logger: 'FS', data: 'toggle: inline->fullscreen' })

      expect(spy).toHaveBeenCalledWith(
        '[mcp-app excalidraw/create_view] info FS:', 'toggle: inline->fullscreen',
      )
      // A notification must not be answered.
      expect(win.postMessage).not.toHaveBeenCalled()
    } finally { spy.mockRestore() }
  })

  it('caps untrusted log content so a hostile app cannot flood the console', () => {
    const spy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    try {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const win = stubContentWindow(container.querySelector('iframe')!)

      send(win, { level: 'info', logger: 'x'.repeat(200), data: 'y'.repeat(10_000) })

      const [prefix, data] = spy.mock.calls.at(-1)!
      expect(data).toHaveLength(2000)
      expect(prefix).toContain('x'.repeat(40))
      expect(prefix).not.toContain('x'.repeat(41))
    } finally { spy.mockRestore() }
  })

  // --- three presentations: inline | wide | overlay --------------------------
  //
  // The ambient ResizeObserver in the test setup is inert (it never fires), and
  // happy-dom reports every width as 0, so the `wide` breakout cannot be observed
  // without driving both: a firing RO plus stubbed layout reads.
  describe('expansion', () => {
    /** A ResizeObserver whose callback we can fire on demand. */
    class FakeResizeObserver {
      static instances: FakeResizeObserver[] = []
      cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) {
        this.cb = cb
        FakeResizeObserver.instances.push(this)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      fire() { this.cb([] as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver) }
    }

    /** Put the frame inside a `.chat-container` scroller and give the scroller a
     *  real clientWidth and the column a real rect, so the breakout measurement
     *  has something to read under happy-dom's zero layout. */
    function mountInScroller({ pane = 1600, column = 900 } = {}) {
      const restore: Array<() => void> = []

      const origRO = globalThis.ResizeObserver
      FakeResizeObserver.instances = []
      globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
      restore.push(() => { globalThis.ResizeObserver = origRO })

      const origClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
      Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
        configurable: true,
        get() { return this.classList?.contains('chat-container') ? pane : 0 },
      })
      restore.push(() => {
        if (origClientWidth) Object.defineProperty(HTMLElement.prototype, 'clientWidth', origClientWidth)
      })

      const origRect = HTMLElement.prototype.getBoundingClientRect
      HTMLElement.prototype.getBoundingClientRect = function () {
        // The scroller starts at x=0; the centred column is inset within it.
        const isScroller = (this as HTMLElement).classList?.contains('chat-container')
        const left = isScroller ? 0 : (pane - column) / 2
        const width = isScroller ? pane : column
        return { width, height: 0, top: 0, left, right: left + width, bottom: 0, x: left, y: 0, toJSON: () => {} } as DOMRect
      }
      restore.push(() => { HTMLElement.prototype.getBoundingClientRect = origRect })

      const scroller = document.createElement('div')
      scroller.className = 'chat-container'
      // The real transcript scroller sets this inline; the overlay's scroll lock
      // resolves its target by computed overflow, so the fixture needs it too.
      scroller.style.overflowY = 'auto'
      // Mirror the real nesting: the scroll area contains a centred, width-capped
      // column, and the frame lives inside that. Without the column the measured
      // "column" would BE the pane and there would be nothing to break out of.
      const columnEl = document.createElement('div')
      scroller.appendChild(columnEl)
      document.body.appendChild(scroller)
      restore.push(() => { scroller.remove() })

      const view = renderWithProviders(<McpAppFrame payload={payload()} />, { container: columnEl })
      return { view, cleanup: () => { restore.reverse().forEach((f) => f()) } }
    }

    it('grows left/right as well as down when expanded to `wide`', () => {
      const { view, cleanup } = mountInScroller({ pane: 1600, column: 900 })
      try {
        const frame = view.container.querySelector('iframe')!
        const wrapper = frame.parentElement as HTMLElement
        // Inline: no width override — the column's own max-width governs.
        expect(wrapper.style.width).toBe('')

        act(() => { view.getByLabelText('Expand app').click() })

        // 1600 pane - 2*24 gutter = 1552, i.e. genuinely wider than the 900 column.
        expect(wrapper.style.width).toBe('1552px')
        // ...and shifted left out of the centred column so it grows BOTH ways.
        expect(wrapper.style.marginLeft).toBe('-326px')
      } finally { cleanup() }
    })

    it('tells the app the wide width, not just the height', () => {
      const { view, cleanup } = mountInScroller({ pane: 1600, column: 900 })
      try {
        const win = stubContentWindow(view.container.querySelector('iframe')!)
        act(() => { view.getByLabelText('Expand app').click() })

        const dims = win.postMessage.mock.calls
          .map(([m]) => m)
          .filter((m) => m.method === 'ui/notifications/host-context-changed')
          .at(-1)!.params.containerDimensions
        expect(dims.width).toBe(1552)
        expect(dims.height).toBeGreaterThan(0)
      } finally { cleanup() }
    })

    it('does not break out when the pane is no wider than the column', () => {
      const { view, cleanup } = mountInScroller({ pane: 900, column: 900 })
      try {
        const wrapper = view.container.querySelector('iframe')!.parentElement as HTMLElement
        act(() => { view.getByLabelText('Expand app').click() })
        expect(wrapper.style.width).toBe('')
        expect(wrapper.style.marginLeft).toBe('')
      } finally { cleanup() }
    })

    it('promotes to a fixed overlay only from the host control', () => {
      const { container, getByLabelText } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const wrapper = container.querySelector('iframe')!.parentElement as HTMLElement
      expect(wrapper.className).not.toContain('fixed')

      act(() => { getByLabelText('Open app full screen').click() })

      expect(wrapper.className).toContain('fixed')
      expect(wrapper.getAttribute('role')).toBe('dialog')
      expect(wrapper.getAttribute('aria-modal')).toBe('true')
      // Body scroll is locked while the sheet is open.
      expect(document.body.style.overflowY).toBe('hidden')

      act(() => { getByLabelText('Exit full screen').click() })
      expect(wrapper.className).not.toContain('fixed')
      expect(document.body.style.overflowY).not.toBe('hidden')
    })

    it('an app asking for fullscreen gets `wide`, never the overlay', () => {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      const win = stubContentWindow(iframe)

      act(() => {
        dispatchFromApp(
          { jsonrpc: '2.0', id: 9, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
          win,
        )
      })

      // The app is told `fullscreen` (its own gate), but the host stayed in the
      // transcript — an app must not be able to throw a modal over the user.
      const reply = win.postMessage.mock.calls.map(([m]) => m).find((m) => m.id === 9)!
      expect(reply.result).toEqual({ mode: 'fullscreen' })
      expect((iframe.parentElement as HTMLElement).className).not.toContain('fixed')
    })

    // The sheet is the user's choice in BOTH directions. Granting `wide` here
    // would demote — closing an overlay the user deliberately opened — on nothing
    // more than an app re-render.
    it('an app re-requesting fullscreen does not demote the user out of the sheet', () => {
      const { container, getByLabelText } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      const wrapper = iframe.parentElement as HTMLElement
      const win = stubContentWindow(iframe)

      act(() => { getByLabelText('Open app full screen').click() })
      expect(wrapper.className).toContain('fixed')

      act(() => {
        dispatchFromApp(
          { jsonrpc: '2.0', id: 11, method: 'ui/request-display-mode', params: { mode: 'fullscreen' } },
          win,
        )
      })

      expect(wrapper.className).toContain('fixed')
      const reply = win.postMessage.mock.calls.map(([m]) => m).find((m) => m.id === 11)!
      expect(reply.result).toEqual({ mode: 'fullscreen' })
    })

    it('an app asking for inline still collapses the sheet (its own Esc path)', () => {
      const { container, getByLabelText } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      const wrapper = iframe.parentElement as HTMLElement
      const win = stubContentWindow(iframe)

      act(() => { getByLabelText('Open app full screen').click() })
      expect(wrapper.className).toContain('fixed')

      act(() => {
        dispatchFromApp(
          { jsonrpc: '2.0', id: 12, method: 'ui/request-display-mode', params: { mode: 'inline' } },
          win,
        )
      })

      expect(wrapper.className).not.toContain('fixed')
    })

    it('escape from the overlay restores the previous presentation, not inline', () => {
      const { view, cleanup } = mountInScroller({ pane: 1600, column: 900 })
      try {
        const wrapper = view.container.querySelector('iframe')!.parentElement as HTMLElement
        act(() => { view.getByLabelText('Expand app').click() })
        expect(wrapper.style.width).toBe('1552px')

        act(() => { view.getByLabelText('Open app full screen').click() })
        expect(wrapper.className).toContain('fixed')

        act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })

        // Back to `wide`, the state it was promoted from.
        expect(wrapper.className).not.toContain('fixed')
        expect(wrapper.style.width).toBe('1552px')
      } finally { cleanup() }
    })

    // The transcript is virtualized, so letting it scroll behind the sheet can
    // unmount the originating row — destroying the iframe and reloading the app
    // with the user's work. Freezing `document.body` does NOT prevent that,
    // because the transcript scrolls its own container.
    it('freezes the transcript scroller, not the body, while the sheet is open', () => {
      const { view, cleanup } = mountInScroller()
      try {
        const scroller = document.querySelector('.chat-container') as HTMLElement
        expect(scroller.style.overflowY).not.toBe('hidden')

        act(() => { view.getByLabelText('Open app full screen').click() })
        expect(scroller.style.overflowY).toBe('hidden')

        act(() => { view.getByLabelText('Exit full screen').click() })
        expect(scroller.style.overflowY).not.toBe('hidden')
      } finally { cleanup() }
    })

    it('releases the scroll lock if the frame unmounts while still expanded', () => {
      const { view, cleanup } = mountInScroller()
      try {
        const scroller = document.querySelector('.chat-container') as HTMLElement
        act(() => { view.getByLabelText('Open app full screen').click() })
        expect(scroller.style.overflowY).toBe('hidden')

        act(() => { view.unmount() })
        expect(scroller.style.overflowY).not.toBe('hidden')
      } finally { cleanup() }
    })

    // Frozen overflow stops the wheel but NOT the follow controller's
    // programmatic scrollTop writes during streaming — and the virtualizer
    // derives its mounted window from scroll position, so an unattended write
    // could unmount the originating row and reload the app mid-edit.
    it('reverts a programmatic scroll while the sheet is open, and stops on release', () => {
      const { view, cleanup } = mountInScroller()
      try {
        const scroller = document.querySelector('.chat-container') as HTMLElement
        scroller.scrollTop = 100

        act(() => { view.getByLabelText('Open app full screen').click() })

        // Simulate the follow controller moving the transcript.
        scroller.scrollTop = 4000
        scroller.dispatchEvent(new Event('scroll'))
        expect(scroller.scrollTop).toBe(100)

        // After release the transcript follows again.
        act(() => { view.getByLabelText('Exit full screen').click() })
        scroller.scrollTop = 4000
        scroller.dispatchEvent(new Event('scroll'))
        expect(scroller.scrollTop).toBe(4000)
      } finally { cleanup() }
    })

    it('keeps the app itself reachable by keyboard inside the overlay', () => {
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const iframe = container.querySelector('iframe')!
      // The focus trap's FOCUSABLE selector matches [tabindex]:not([tabindex="-1"]);
      // without an explicit tab stop the trap would cycle on the header button
      // alone and the canvas would be unreachable.
      expect(iframe.getAttribute('tabindex')).toBe('0')
      expect(iframe.matches('[tabindex]:not([tabindex="-1"])')).toBe(true)
    })

    // The global shortcut layer binds Ctrl/Alt+digit to chat jumps, and switching
    // session unmounts the frame — reloading the app and losing the user's work
    // from a keystroke aimed at a sheet covering the whole viewport.
    it('contains keystrokes while the sheet is open, but not Escape', () => {
      const { container, getByLabelText } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const wrapper = container.querySelector('iframe')!.parentElement as HTMLElement
      const globalSpy = vi.fn()
      window.addEventListener('keydown', globalSpy) // bubble phase, like the shortcut layer
      try {
        // Inline: the transcript is normal, shortcuts must keep working.
        act(() => {
          wrapper.dispatchEvent(new KeyboardEvent('keydown', { key: '1', ctrlKey: true, bubbles: true }))
        })
        expect(globalSpy).toHaveBeenCalledTimes(1)

        act(() => { getByLabelText('Open app full screen').click() })
        globalSpy.mockClear()

        act(() => {
          wrapper.dispatchEvent(new KeyboardEvent('keydown', { key: '1', ctrlKey: true, bubbles: true }))
        })
        expect(globalSpy).not.toHaveBeenCalled()

        // Escape still dismisses — the focus trap listens in the capture phase.
        act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
        expect(wrapper.className).not.toContain('fixed')
      } finally { window.removeEventListener('keydown', globalSpy) }
    })

    // Two frames sharing one scroller must not unfreeze each other: the first
    // release would re-arm the virtualization data loss for the sheet still open,
    // and the last could write a stale value back and freeze the transcript for
    // good. The lock is therefore ref-counted, not per-instance.
    it('holds the scroll lock until the LAST overlay releases it', () => {
      const restore: Array<() => void> = []
      const origRect = HTMLElement.prototype.getBoundingClientRect
      restore.push(() => { HTMLElement.prototype.getBoundingClientRect = origRect })

      const scroller = document.createElement('div')
      scroller.className = 'chat-container'
      scroller.style.overflowY = 'auto'
      const colA = document.createElement('div')
      const colB = document.createElement('div')
      scroller.append(colA, colB)
      document.body.appendChild(scroller)
      restore.push(() => { scroller.remove() })

      try {
        renderWithProviders(<McpAppFrame payload={payload()} />, { container: colA })
        renderWithProviders(<McpAppFrame payload={payload()} />, { container: colB })
        // Bound queries search document.body, which now holds BOTH frames — so
        // each control has to be reached through its own column.
        const control = (col: HTMLElement, label: string) =>
          col.querySelector<HTMLElement>(`[aria-label="${label}"]`)!

        act(() => { control(colA, 'Open app full screen').click() })
        expect(scroller.style.overflowY).toBe('hidden')
        act(() => { control(colB, 'Open app full screen').click() })

        // First release must NOT unfreeze — the second sheet is still open.
        act(() => { control(colA, 'Exit full screen').click() })
        expect(scroller.style.overflowY).toBe('hidden')

        // Last release restores the original value.
        act(() => { control(colB, 'Exit full screen').click() })
        expect(scroller.style.overflowY).not.toBe('hidden')
      } finally { restore.reverse().forEach((f) => f()) }
    })
  })
})


// The app has no other styling signal: this host injects no CSS into the srcdoc,
// so `hostContext.theme` is the whole contract. It was a hardcoded 'dark', which
// left every MCP app dark-on-light for a light-theme user (#2110). The
// pre-existing initialize test asserts displayMode, availableDisplayModes and
// containerDimensions but never theme -- that gap is how this shipped, so these
// assert the field itself.
describe('McpAppFrame — host theme', () => {
  beforeEach(() => {
    __resetRevealedForTests()
    localStorage.clear()
  })

  function initReply(win: { postMessage: ReturnType<typeof vi.fn> }) {
    dispatchFromApp({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' }, win)
    return win.postMessage.mock.calls[0][0]
  }

  it.each(['light', 'dark'] as const)(
    'reports the host theme as %s in the ui/initialize reply',
    (mode) => {
      // An explicit preference resolves synchronously, with no matchMedia dependency.
      localStorage.setItem('mc-theme', mode)
      const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
      const win = stubContentWindow(container.querySelector('iframe')!)

      expect(initReply(win).result.hostContext.theme).toBe(mode)
    },
  )

  it('pushes a host-context-changed carrying the new theme when the user switches', async () => {
    localStorage.setItem('mc-theme', 'dark')
    // Sibling consumer inside the same ThemeProvider, so the flip goes through
    // the real setter the theme toggle uses rather than a synthetic event.
    function ThemeFlip() {
      const { setTheme } = useTheme()
      return <button onClick={() => setTheme('light')}>flip</button>
    }
    const { container, getByText } = renderWithProviders(
      <><ThemeFlip /><McpAppFrame payload={payload()} /></>,
    )
    const win = stubContentWindow(container.querySelector('iframe')!)
    initReply(win)
    win.postMessage.mockClear()

    await act(async () => { getByText('flip').click() })

    const notes = win.postMessage.mock.calls
      .map(([m]) => m)
      .filter((m) => m?.method === 'ui/notifications/host-context-changed')
    expect(notes.length).toBeGreaterThan(0)
    expect(notes[notes.length - 1].params.theme).toBe('light')
  })

  it('does not announce a theme change on first mount', () => {
    localStorage.setItem('mc-theme', 'light')
    const { container } = renderWithProviders(<McpAppFrame payload={payload()} />)
    const win = stubContentWindow(container.querySelector('iframe')!)

    // Nothing is posted before the app speaks: a spurious notification on mount
    // (what a first-run boolean flag would produce under StrictMode's double
    // effect invocation) would show up here.
    expect(win.postMessage).not.toHaveBeenCalled()
  })
})
