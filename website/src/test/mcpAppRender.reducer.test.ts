import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { sseMcpAppRender, mcpAppKey, clearMessages, setActiveSlot } from '../store/chatSlice'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'

function payload(over: Partial<McpAppRenderPayload> = {}): McpAppRenderPayload {
  return {
    session_key: 'slot-1',
    tool_call_id: 'call-abc',
    server: 'excalidraw',
    tool: 'create_view',
    html: '<html><head></head><body>app</body></html>',
    csp: null,
    permissions: null,
    spool_id: 'uuid-1',
    ...over,
  }
}

const KEY = mcpAppKey('slot-1', 'call-abc')

describe('sseMcpAppRender reducer', () => {
  it('stores the payload keyed by (session_key, tool_call_id)', () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender(payload()))
    expect(store.getState().chat.mcpApps[KEY]).toMatchObject({
      server: 'excalidraw',
      tool: 'create_view',
      spool_id: 'uuid-1',
    })
    // The bare tool_call_id is NOT a key — cross-session reuse of an ACP id
    // can never collide (GPT 5.6 finding: session-scoped keying).
    expect(store.getState().chat.mcpApps['call-abc']).toBeUndefined()
  })

  it('keeps same tool_call_id in different sessions as separate entries', () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender(payload({ session_key: 'slot-1', html: '<body>a</body>' })))
    store.dispatch(sseMcpAppRender(payload({ session_key: 'slot-2', html: '<body>b</body>' })))
    expect(store.getState().chat.mcpApps[mcpAppKey('slot-1', 'call-abc')].html).toBe('<body>a</body>')
    expect(store.getState().chat.mcpApps[mcpAppKey('slot-2', 'call-abc')].html).toBe('<body>b</body>')
  })

  it('overwrites an existing entry for the same (session, tool_call_id)', () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender(payload({ html: '<body>v1</body>' })))
    store.dispatch(sseMcpAppRender(payload({ html: '<body>v2</body>' })))
    expect(store.getState().chat.mcpApps[KEY].html).toBe('<body>v2</body>')
    expect(Object.keys(store.getState().chat.mcpApps)).toHaveLength(1)
  })

  it('ignores a payload with no tool_call_id or no session_key', () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender(payload({ tool_call_id: '' })))
    store.dispatch(sseMcpAppRender(payload({ session_key: '' })))
    expect(Object.keys(store.getState().chat.mcpApps)).toHaveLength(0)
  })

  it('never writes a prototype-polluting key', () => {
    const store = createTestStore()
    store.dispatch(sseMcpAppRender(payload({ tool_call_id: '__proto__' })))
    store.dispatch(sseMcpAppRender(payload({ session_key: '__proto__' })))
    expect(Object.prototype.hasOwnProperty.call(store.getState().chat.mcpApps, '__proto__')).toBe(false)
    expect(Object.keys(store.getState().chat.mcpApps)).toHaveLength(0)
  })

  it('clearMessages evicts the active slot entries only (memory bound)', () => {
    // Payloads carry multi-MB app HTML — clearing a conversation must drop
    // its entries so Redux does not grow for the dashboard lifetime.
    const store = createTestStore()
    store.dispatch(setActiveSlot('slot-1'))
    store.dispatch(sseMcpAppRender(payload({ session_key: 'slot-1' })))
    store.dispatch(sseMcpAppRender(payload({ session_key: 'slot-2' })))
    store.dispatch(clearMessages())
    const apps = store.getState().chat.mcpApps
    expect(apps[mcpAppKey('slot-1', 'call-abc')]).toBeUndefined()
    expect(apps[mcpAppKey('slot-2', 'call-abc')]).toBeDefined()
  })
})
