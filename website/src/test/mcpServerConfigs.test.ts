import { describe, it, expect } from 'vitest'
import { mcpServerConfigs } from '../components/appstore/types'

describe('mcpServerConfigs', () => {
  it('keeps a server named `__proto__` as a listed entry', () => {
    // As the API delivers it: JSON.parse keeps `__proto__` as an own key, and the
    // backend registers and launches that server, so the page must list it.
    const raw = JSON.parse('{"__proto__": {"command": "proto-mcp"}, "ok": {"url": "http://127.0.0.1:9/mcp"}}')
    const out = mcpServerConfigs(raw)!
    expect(Object.keys(out).sort()).toEqual(['__proto__', 'ok'])
    expect(out['__proto__'].command).toBe('proto-mcp')
  })

  it('drops non-object entries and types the fields the card renders', () => {
    const out = mcpServerConfigs({ broken: null, text: 'nope', ok: { command: 7, autoApprove: ['read', 3] } })!
    expect(Object.keys(out)).toEqual(['ok'])
    expect(out.ok.command).toBeUndefined()
    expect(out.ok.autoApprove).toEqual(['read'])
  })
})
