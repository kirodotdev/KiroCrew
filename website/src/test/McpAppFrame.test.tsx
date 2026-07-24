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
    expect(reply.result.protocolVersion).toBe('2026-01-26')
    expect(reply.result.hostInfo).toEqual({ name: 'kirocrew', version: '0.1' })
    expect(reply.result.hostContext.displayMode).toBe('inline')
    expect(reply.result.hostContext.availableDisplayModes).toEqual(['inline'])
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
