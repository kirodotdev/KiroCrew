/**
 * Safety switches (Settings -> Security): the owner's toggles for
 * `mcp.protect_kiro_permission_rules` and `mcp.honour_auto_approve`.
 *
 * The card is a VIEW over `/api/security/safety-switches`; the backend's rules
 * are in test/test_safety_switches_routes.py. Pinned here: both toggles draw the
 * stored values, a click sends exactly that switch's key, a non-owner sees them
 * disabled, a degraded read says so, and a failed read draws no switch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { SafetySwitchesState } from '../../api/client'
import { ApiError } from '../../api/apiError'

vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
  api: {
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    safetySwitches: vi.fn(),
    setSafetySwitch: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>

function state(protect: boolean, honour: boolean, degraded = false, protectInForce = protect): SafetySwitchesState {
  return {
    degraded,
    switches: [
      { key: 'mcp.protect_kiro_permission_rules', enabled: protect, in_force: protectInForce },
      { key: 'mcp.honour_auto_approve', enabled: honour, in_force: honour },
    ],
  }
}

function render() {
  return renderWithProviders(<SecurityPanel />, { route: '/?section=safety', queryDefaults: { retryDelay: 0 } })
}

const protectSwitch = () => within(screen.getByTestId('safety-switch-protect')).getByRole('switch')
const honourSwitch = () => within(screen.getByTestId('safety-switch-honour')).getByRole('switch')

describe('SecurityPanel - safety switches', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mock(api.deniedCommands).mockResolvedValue({ builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false })
    mock(api.securityPosture).mockResolvedValue({ controls: [], counts: {} })
    mock(api.governancePolicy).mockResolvedValue({ version: null, has_policy: false, profile: null, unavailable: false, scopes: [] })
    mock(api.kirocrewConfig).mockResolvedValue({})
    mock(api.tailnetStatus).mockResolvedValue({ enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off' })
  })

  it('draws both toggles with the stored values', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(true, false))
    render()
    await screen.findByTestId('safety-switches-rows')
    expect(screen.getByText('Stop the agent from editing its own permission rules')).toBeTruthy()
    expect(screen.getByText("Ask me before running tools set to auto-approve in a tool server's settings")).toBeTruthy()
    expect(screen.getByText('Tools you set to Always allow, on an approval card or in MCP Servers, still run without asking.')).toBeTruthy()
    // The protect help leads with what On does; the terminal caveat is its own line.
    const protectRow = screen.getByTestId('safety-switch-protect')
    expect(within(protectRow).getByText("On: the agent's file-editing tools can't change the rules that let tools run without asking you. Off: they can.")).toBeTruthy()
    expect(within(protectRow).getByTestId('safety-switch-terminal-note').textContent)
      .toBe('This does not cover commands the agent runs in a terminal: they can change these rules whether this is on or off.')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'true')
    // The ask toggle is the INVERSE of mcp.honour_auto_approve, so on is safer for both.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(protectSwitch()).not.toHaveAttribute('aria-disabled')
    expect(screen.queryByTestId('safety-switches-degraded')).toBeNull()
  })

  it('a click sends exactly that switch key and the new value', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockResolvedValue(state(true, true))
    render()
    await screen.findByTestId('safety-switches-rows')
    mock(api.safetySwitches).mockResolvedValue(state(true, true))
    fireEvent.click(protectSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenCalledWith('mcp.protect_kiro_permission_rules', true))
    await waitFor(() => expect(protectSwitch()).toHaveAttribute('aria-checked', 'true'))

    // honour is on (true), so the ask toggle reads off; turning it ON sends honour=false.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'false')
    mock(api.setSafetySwitch).mockResolvedValue(state(true, false))
    mock(api.safetySwitches).mockResolvedValue(state(true, false))
    fireEvent.click(honourSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenLastCalledWith('mcp.honour_auto_approve', false))
    expect(api.setSafetySwitch).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(honourSwitch()).toHaveAttribute('aria-checked', 'true'))
  })

  it('the ask row always says it applies after a restart; the protect row does not', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(true, true))
    mock(api.setSafetySwitch).mockResolvedValue(state(false, true))
    render()
    await screen.findByTestId('safety-switches-rows')
    const askRow = screen.getByTestId('safety-switch-honour')
    expect(within(askRow).getByTestId('safety-switch-restart-note')).toBeTruthy()
    // An info icon, not a reload arrow: the line is a fact, not an action.
    expect(within(askRow).getByTestId('safety-switch-restart-note').querySelector('svg.lucide-info')).not.toBeNull()
    expect(within(screen.getByTestId('safety-switch-protect')).queryByTestId('safety-switch-restart-note')).toBeNull()
    // Not tied to the last change: flipping the OTHER switch keeps it.
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    fireEvent.click(protectSwitch())
    await waitFor(() => expect(protectSwitch()).toHaveAttribute('aria-checked', 'false'))
    expect(within(screen.getByTestId('safety-switch-honour')).getByTestId('safety-switch-restart-note')).toBeTruthy()
  })

  it('a non-owner sees both toggles disabled, from the config they can read, with the owner-only notice', async () => {
    mock(api.safetySwitches).mockRejectedValue(new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })))
    mock(api.kirocrewConfig).mockResolvedValue({ mcp: { protect_kiro_permission_rules: false, honour_auto_approve: true } })
    render()
    await screen.findByTestId('safety-switches-owner-only')
    await screen.findByTestId('safety-switch-protect')
    expect(protectSwitch()).toHaveAttribute('aria-disabled', 'true')
    expect(honourSwitch()).toHaveAttribute('aria-disabled', 'true')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'false')
    // honour_auto_approve true in config -> the ask toggle reads off.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'false')
    // A permission lock, not an error: neutral, no ask-agent hand-off.
    expect(screen.queryByText('Open a chat with the agent about this error')).toBeNull()
    expect(within(screen.getByTestId('safety-switches-rows')).queryByRole('alert')).toBeNull()
    // A refusal is a fact about the viewer: read once, never retried.
    expect(api.safetySwitches).toHaveBeenCalledTimes(1)
    fireEvent.click(protectSwitch())
    expect(api.setSafetySwitch).not.toHaveBeenCalled()
    // Dimmed switches look alike, so each disabled row says On or Off in text.
    // The config holds SAVED values, not the state in force, so the line says Saved.
    expect(screen.getByTestId('mcp.protect_kiro_permission_rules-state').textContent).toBe('Saved: off')
    expect(screen.getByTestId('mcp.honour_auto_approve-state').textContent).toBe('Saved: off')
    expect(screen.queryByText('Currently off')).toBeNull()
    // A saved value is never presented as in force.
    expect(screen.queryByText(/right now/)).toBeNull()
  })

  it('a non-owner whose config read fails sees the failure, not stale rows', async () => {
    mock(api.safetySwitches).mockRejectedValue(new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })))
    mock(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    render()
    await screen.findByTestId('safety-switches-config-read-failed', {}, { timeout: 5000 })
    expect(screen.queryByTestId('safety-switch-protect')).toBeNull()
    expect(screen.queryByTestId('safety-switch-honour')).toBeNull()
  })

  it('a degraded read draws each toggle in its state in force and names what is saved', async () => {
    // protect saved off but in force on; the toggle shows the state in force.
    mock(api.safetySwitches).mockResolvedValue(state(false, true, true, true))
    render()
    await screen.findByTestId('safety-switches-degraded')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(within(screen.getByTestId('safety-switch-protect')).getByText(
      "On for now, because Kiro Crew could not read its settings in full. Clicking won't turn it off before then.",
    )).toBeTruthy()
    // The section notice names the manual fix and offers Retry, and does not repeat the row note.
    const notice = screen.getByTestId('safety-switches-degraded')
    expect(notice.textContent).toMatch(/Fix the mistake in config\.json or config\.local\.json \(in ~\/\.kiro\/crew by default\), then restart Kiro Crew\./)
    expect(notice.textContent).not.toMatch(/Ask the agent to fix it/)
    expect(within(notice).queryByText(/saved as/)).toBeNull()
    expect(screen.getByTestId('safety-switches-degraded-retry')).toBeTruthy()
  })

  it('the in-force note follows the shown (inverted) ask toggle', async () => {
    // honour saved on (ask saved off) but in force off (ask in force on).
    mock(api.safetySwitches).mockResolvedValue({
      degraded: true,
      switches: [
        { key: 'mcp.protect_kiro_permission_rules', enabled: true, in_force: true },
        { key: 'mcp.honour_auto_approve', enabled: true, in_force: false },
      ],
    })
    mock(api.setSafetySwitch).mockResolvedValue(state(true, true))
    render()
    await screen.findByTestId('safety-switches-degraded')
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(within(screen.getByTestId('safety-switch-honour')).getByText(
      "On for now, because Kiro Crew could not read its settings in full. Clicking won't turn it off before then.",
    )).toBeTruthy()
    // A click saves the ask toggle Off (honour_auto_approve=true).
    fireEvent.click(honourSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenCalledWith('mcp.honour_auto_approve', true))
  })

  it('a switch off in force while On is saved says so, and what a click saves', async () => {
    // protect saved on but not in force: only a config change between the two reads shows this.
    mock(api.safetySwitches).mockResolvedValue(state(true, true, false, false))
    mock(api.setSafetySwitch).mockResolvedValue(state(true, true))
    render()
    await screen.findByTestId('safety-switches-rows')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'false')
    expect(within(screen.getByTestId('safety-switch-protect')).getByText('Off right now, though On is saved. Clicking saves On.')).toBeTruthy()
    fireEvent.click(protectSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenCalledWith('mcp.protect_kiro_permission_rules', true))
  })

  it('a save refused for a pending outside edit says to review it, under that row', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockRejectedValue(new ApiError(409, 'pending', JSON.stringify({ code: 'file_pending_review' })))
    render()
    await screen.findByTestId('safety-switches-rows')
    fireEvent.click(honourSwitch())
    const notice = await within(screen.getByTestId('safety-switch-honour')).findByTestId('safety-switches-write-failed')
    expect(notice.textContent).toMatch(/changed outside/)
    expect(within(screen.getByTestId('safety-switch-protect')).queryByTestId('safety-switches-write-failed')).toBeNull()
  })

  it('a failed read draws no switch and offers Retry', async () => {
    mock(api.safetySwitches).mockRejectedValue(new Error('boom'))
    render()
    await screen.findByTestId('safety-switches-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.getByTestId('safety-switches-retry')).toBeTruthy()
  })

  it('a refused write keeps the position and says so', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockRejectedValue(new ApiError(503, 'audit unavailable', JSON.stringify({ code: 'safety_switch_audit_unavailable' })))
    render()
    await screen.findByTestId('safety-switches-rows')
    fireEvent.click(protectSwitch())
    const notice = await within(screen.getByTestId('safety-switch-protect')).findByTestId('safety-switches-write-failed')
    expect(notice.textContent).toMatch(/Could not save the change/)
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'false')
  })
})
