/**
 * The model-cycle shortcuts step through the same model list the active chat's
 * picker filled. A degraded pick runs on Kiro, so its picker fills Kiro's list;
 * reading the raw pick there found no cached list and the shortcut did nothing.
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))

import { ACP_BACKEND_KIRO } from '../api/acpBackend'
import { activeModelsBackend } from '../shell/shortcuts/shellKeyboard'

const slots = [
  { key: 'plain', acp_backend: null },
  { key: 'picked', acp_backend: 'kas' },
  { key: 'degraded', acp_backend: 'codex', acp_backend_degraded: true },
]

describe('activeModelsBackend', () => {
  it('keys a chat with no pick, or no such chat, on the configured list', () => {
    expect(activeModelsBackend(slots, 'plain')).toBeNull()
    expect(activeModelsBackend(slots, 'missing')).toBeNull()
  })

  it("keys a selectable pick on that backend's list", () => {
    expect(activeModelsBackend(slots, 'picked')).toBe('kas')
  })

  it("keys a degraded pick on Kiro's list, the one its picker fills", () => {
    expect(activeModelsBackend(slots, 'degraded')).toBe(ACP_BACKEND_KIRO)
  })
})
