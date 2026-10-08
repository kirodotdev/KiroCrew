/**
 * Which backend's model list a chat's picker shows. A degraded pick is outside
 * the selectable set and the gateway runs the chat on Kiro, so the gateway would
 * refuse the pick's own list on every poll: Kiro's list is the one that applies.
 */
import { describe, expect, it } from 'vitest'
import { ACP_BACKEND_KIRO, backendPickable, modelsBackendFor } from '../api/acpBackend'

describe('modelsBackendFor', () => {
  it('keys a chat with no pick on the configured list', () => {
    expect(modelsBackendFor(undefined)).toBeNull()
    expect(modelsBackendFor({ acp_backend: null })).toBeNull()
  })

  it('keys a selectable pick on its own list', () => {
    expect(modelsBackendFor({ acp_backend: 'kas' })).toBe('kas')
    expect(modelsBackendFor({ acp_backend: ACP_BACKEND_KIRO })).toBe(ACP_BACKEND_KIRO)
  })

  it('keys a degraded pick on Kiro, the backend the chat runs on', () => {
    expect(modelsBackendFor({ acp_backend: 'codex', acp_backend_degraded: true })).toBe(ACP_BACKEND_KIRO)
  })
})

describe('backendPickable', () => {
  it('offers a picker on an ordinary chat', () => {
    expect(backendPickable({}, false)).toBe(true)
  })

  it('hides it with no chat, on a peer-bound chat, and on a channel-linked chat', () => {
    expect(backendPickable(undefined, false)).toBe(false)
    expect(backendPickable({}, true)).toBe(false)
    expect(backendPickable({ acp_backend_channel_bound: true }, false)).toBe(false)
  })
})
