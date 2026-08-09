import { describe, it, expect } from 'vitest'
import { mcpAppsSwitchState } from '../pages/settings/SharedMcpGatewayToggle'

function state(partial: Partial<Parameters<typeof mcpAppsSwitchState>[0]> = {}) {
  return mcpAppsSwitchState({
    gatewayEnabled: true,
    appsEnabled: true,
    loading: false,
    busy: false,
    ...partial,
  })
}

describe('mcpAppsSwitchState', () => {
  it('is togglable when the broker is on', () => {
    expect(state()).toEqual({ checked: true, disabled: false, needsGateway: false })
  })

  it('stays settable while the broker is OFF so the opt-out can be pre-recorded', () => {
    // The load-bearing case. `apps_enabled` defaults on, so gating this behind a
    // running broker would force a cautious user to enable the broker first —
    // exposing themselves to server-authored UI — and then race to switch it off.
    // The endpoint writes config only and needs no broker.
    expect(state({ gatewayEnabled: false }).disabled).toBe(false)
  })

  it('flags needsGateway only when the broker is off', () => {
    expect(state({ gatewayEnabled: false }).needsGateway).toBe(true)
    expect(state({ gatewayEnabled: true }).needsGateway).toBe(false)
  })

  it('reports the stored state regardless of the broker', () => {
    expect(state({ gatewayEnabled: false, appsEnabled: true }).checked).toBe(true)
    expect(state({ gatewayEnabled: false, appsEnabled: false }).checked).toBe(false)
  })

  it('disables only while loading or applying', () => {
    expect(state({ loading: true }).disabled).toBe(true)
    expect(state({ busy: true }).disabled).toBe(true)
  })

  it('exposes no per-state description', () => {
    // The row describes what the switch CONTROLS, not what is happening, so a
    // state-dependent description would be the thing that misreports the trust
    // fact this control exists to answer. Re-adding one should fail here.
    expect(Object.keys(state()).sort()).toEqual(['checked', 'disabled', 'needsGateway'])
  })
})
