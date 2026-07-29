import { describe, it, expect, vi } from 'vitest'
import { renderWithProviders } from './helpers'
import { act } from '@testing-library/react'
import McpAppFrame from '../components/McpAppFrame'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'

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
    // GPT 5.6 finding: apps that initialize from their inputs must get the
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
      // #418: the callback capability the gateway authorizes on is forwarded,
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
    // GPT 5.6 finding: a navigated-to page's <head> script can post tools/call
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
 * Display-mode negotiation (SEP-1865 `ui/request-display-mode`). Before this was
 * implemented the request fell through to the method-not-found default, so an app
 * that gates its INTERACTIVE surface on `fullscreen` (excalidraw only mounts its
 * editable canvas there) could never leave its static preview — the rendered
 * diagram looked inert and the app's own fullscreen button was dead.
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
 * tools/call and THEN calls openLink. That second call used to hit the -32601
 * default, so the export succeeded and the tab never opened — a silent dead end.
 * The URL comes from sandboxed app content, so it is untrusted input.
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
})
