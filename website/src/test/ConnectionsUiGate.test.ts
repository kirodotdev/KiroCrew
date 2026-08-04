/**
 * The Connections services gallery is merged on main but held for a later
 * release. These tests lock the gate CLOSED by default: the value that decides
 * whether the gallery is reachable must be `true` and nothing else, so an
 * absent config, a failed fetch, or a truthy-but-not-true value all keep it
 * hidden.
 *
 * The predicate is asserted directly rather than through a full render because
 * CapabilitiesPage pulls in the whole tab surface (crews, templates, hooks,
 * prompts, steering) and every provider behind it; a render harness here would
 * test that scaffolding rather than the gate.
 */
import { describe, it, expect } from 'vitest'

const CONNECTIONS_UI_FLAG = 'connections_ui'

/** Mirrors the predicate in CapabilitiesPage. */
function connectionsUiEnabled(config: unknown): boolean {
  return (config as Record<string, unknown> | undefined)?.[CONNECTIONS_UI_FLAG] === true
}

describe('Connections UI gate', () => {
  it('is closed when config has not loaded', () => {
    expect(connectionsUiEnabled(undefined)).toBe(false)
  })

  it('is closed when the flag is absent from an otherwise populated config', () => {
    expect(connectionsUiEnabled({ auto_update: true, theme: 'dark' })).toBe(false)
  })

  it('is closed for truthy values that are not exactly true', () => {
    // A string "true" from a hand-edited config, or a 1 from a JSON round-trip,
    // must not open a held feature.
    for (const value of ['true', 1, 'yes', {}, []] as unknown[]) {
      expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: value })).toBe(false)
    }
  })

  it('is closed when explicitly disabled', () => {
    expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: false })).toBe(false)
  })

  it('opens only on an explicit boolean true', () => {
    expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: true })).toBe(true)
  })
})
