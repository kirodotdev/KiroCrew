/**
 * The Connections services gallery ships ON, with `connections_ui: false` kept as
 * an escape hatch. These tests pin both halves of that: the gate opens for an
 * install that never set the flag, and an instance that explicitly set it false
 * still gets everything hidden.
 *
 * They also pin the LAUNCH SET, which is a separate decision from the gate. The
 * gate says whether a gallery renders; `launch_gate_passed` in the registry says
 * which providers it may offer. GitHub has not passed that gate — its OAuth-app
 * registration is outstanding — so flipping the gate on must not put a GitHub card
 * on screen. Asserting it here, on the exported list, catches a registry edit that
 * a render test would only catch if it happened to name the provider.
 *
 * The predicate is asserted directly rather than through a full render because
 * CapabilitiesPage pulls in the whole tab surface (crews, templates, hooks,
 * prompts, steering) and every provider behind it; a render harness here would
 * test that scaffolding rather than the gate. It is imported from the shared
 * hook rather than mirrored locally, because the chat renderer now reads the
 * same flag to decide whether a Connections card owns an OAuth prompt — a
 * mirrored copy could drift and leave chat hiding a banner on an install where
 * no card exists to replace it.
 */
import { describe, it, expect } from 'vitest'
import { connectionsUiEnabled } from '../hooks/useConnectionsUi'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'

const CONNECTIONS_UI_FLAG = 'connections_ui'

describe('Connections UI gate', () => {
  it('is OPEN when the flag is absent from an otherwise populated config', () => {
    // The shipped default: every install that never touched the flag.
    expect(connectionsUiEnabled({ auto_update: true, theme: 'dark' })).toBe(true)
  })

  it('is OPEN for a config that carries nothing at all', () => {
    expect(connectionsUiEnabled({})).toBe(true)
  })

  it('is OPEN on an explicit boolean true', () => {
    expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: true })).toBe(true)
  })

  it('is CLOSED when explicitly disabled — the escape hatch', () => {
    expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: false })).toBe(false)
  })

  it('is CLOSED until the config has actually been read', () => {
    // `undefined` is both "still loading" and "the fetch failed". The opt-out
    // lives in the config, so an unread config is no basis to ignore it: opening
    // here would flash the gallery at a user who turned it off and fire its
    // status queries on their behalf.
    expect(connectionsUiEnabled(undefined)).toBe(false)
    expect(connectionsUiEnabled(null)).toBe(false)
  })

  it('is CLOSED for a config that is not an object', () => {
    for (const value of ['', 'yes', 0, 7, true] as unknown[]) {
      expect(connectionsUiEnabled(value)).toBe(false)
    }
  })

  it('is CLOSED for present-but-not-exactly-true values', () => {
    // A string "false" from a hand-edited config must actually disable the
    // feature rather than silently reading as "not false, so on". Only an exact
    // `true` re-asserts the default, so every sloppy value fails safe — which
    // is now off.
    for (const value of ['false', 'true', 0, 1, 'yes', {}, []] as unknown[]) {
      expect(connectionsUiEnabled({ [CONNECTIONS_UI_FLAG]: value })).toBe(false)
    }
  })
})

describe('the launched card set', () => {
  it('withholds GitHub, whose launch gate has not passed', () => {
    expect(CONNECTION_PROVIDERS.map(provider => provider.slug)).not.toContain('github')
  })

  it('offers only providers that passed the launch gate and clear vendor approval', () => {
    expect(CONNECTION_PROVIDERS.length).toBeGreaterThan(0)
    for (const provider of CONNECTION_PROVIDERS) {
      expect(provider.launch_gate_passed).toBe(true)
      expect(provider.vendor_approval_pending).toBe(false)
    }
  })
})
