import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { ComputerUseConfigData } from '../../api/client'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these two methods, so mocking them
 * keeps every case network-free (MSW is reserved for `integration/*`). Each
 * save resolves with the snapshot the panel then re-renders from. */
vi.mock('../../api/client', () => ({
  api: {
    getComputerUseConfig: vi.fn(),
    saveComputerUseConfig: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ComputerUsePanel, commitNumericDraft, permissionPollInterval } from './ComputerUsePanel'

const ENABLE_LABEL = /enable computer use/i

function snapshot(overrides: Partial<ComputerUseConfigData> = {}): ComputerUseConfigData {
  return {
    enabled: false,
    supported: true,
    platform: 'macos',
    reason: '',
    max_tree_nodes: 1200,
    max_tree_depth: 64,
    text_limit: 500,
    attach_screenshot: true,
    screenshot_max_px: 1280,
    screenshot_jpeg_quality: 55,
    allowed_apps: [],
    extra_denied_apps: [],
    permissions: { accessibility: 'granted', screen_recording: 'granted', responsible_hint: '' },
    limits: { max_tree_nodes: [1, 5000], screenshot_max_px: [320, 4096] },
    ...overrides,
  }
}

/** Render with the config query pre-resolved and hydrated. */
async function renderPanel(data: ComputerUseConfigData = snapshot()) {
  ;(api.getComputerUseConfig as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  ;(api.saveComputerUseConfig as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<ComputerUsePanel />)
  await screen.findByText('Computer Use')
  return utils
}

describe('ComputerUsePanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('explains the session restart when the server reports one', async () => {
    // The server restarts sessions on an enable flip because kiro-cli caches its
    // tool list per session. An unexplained reset reads as a crash, so the panel
    // has to say why — and must NOT claim one when the server did not do one.
    await renderPanel()
    ;(api.saveComputerUseConfig as ReturnType<typeof vi.fn>).mockResolvedValue(
      snapshot({ enabled: true, sessions_reset: 2 }),
    )
    expect(screen.queryByText(/chat sessions were restarted/i)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('switch', { name: ENABLE_LABEL }))
    expect(await screen.findByText(/chat sessions were restarted/i)).toBeInTheDocument()
  })

  it('stays silent when the save restarted nothing', async () => {
    await renderPanel()
    ;(api.saveComputerUseConfig as ReturnType<typeof vi.fn>).mockResolvedValue(
      snapshot({ enabled: true, sessions_reset: 0 }),
    )
    fireEvent.click(await screen.findByRole('switch', { name: ENABLE_LABEL }))
    await waitFor(() => expect(api.saveComputerUseConfig).toHaveBeenCalled())
    expect(screen.queryByText(/chat sessions were restarted/i)).not.toBeInTheDocument()
  })

  it('renders the primary toggle off by default', async () => {
    await renderPanel()
    const toggle = await screen.findByRole('switch', { name: ENABLE_LABEL })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle).not.toBeDisabled()
  })

  it('saves { enabled: true } when the primary toggle is turned on', async () => {
    await renderPanel()
    fireEvent.click(await screen.findByRole('switch', { name: ENABLE_LABEL }))
    await waitFor(() =>
      expect(api.saveComputerUseConfig).toHaveBeenCalledWith({ enabled: true }),
    )
  })

  it('shows a warn badge and a grant shortcut when Accessibility is missing', async () => {
    await renderPanel(
      snapshot({
        permissions: {
          accessibility: 'missing',
          screen_recording: 'granted',
          responsible_hint: 'Grant them to Terminal',
        },
      }),
    )
    // The badge shows the HUMAN label, not the backend's wire token: `missing`
    // reads as jargon in a status chip, and an operator seeing it cannot tell
    // whether something is broken or simply not granted yet.
    expect(await screen.findByText('Not detected')).toBeInTheDocument()
    expect(screen.queryByText('missing')).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /open system settings for accessibility/i }),
    ).toBeInTheDocument()
    expect(screen.getByText('Grant them to Terminal')).toBeInTheDocument()
  })

  it('renders only the reason and no toggle when the platform is unsupported', async () => {
    await renderPanel(
      snapshot({
        supported: false,
        platform: 'linux',
        reason: 'the Linux AT-SPI driver is not implemented yet',
      }),
    )
    expect(
      await screen.findByText('the Linux AT-SPI driver is not implemented yet'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: ENABLE_LABEL })).not.toBeInTheDocument()
  })



  /* ── the real-pointer opt-in ────────────────────────────────────────────── */


  /* ── numeric limits ─────────────────────────────────────────────────────── */

  describe('commitNumericDraft', () => {
    const NODE_BOUNDS: [number, number] = [1, 5000]

    it('discards an empty field instead of resolving to the floor', () => {
      // `Number('')` is 0, and clamping 0 to the published range yields the FLOOR
      // (1 node / 320px). A user who selects-all-and-retypes passes through the
      // empty state, so the old code transiently saved a 1-node accessibility
      // tree. `null` means "discard", which keeps the persisted value.
      expect(commitNumericDraft('', 1200, NODE_BOUNDS)).toBeNull()
      expect(commitNumericDraft('   ', 1200, NODE_BOUNDS)).toBeNull()
      expect(commitNumericDraft('', 1280, [320, 4096])).toBeNull()
    })

    it('discards an untouched draft and an unparseable one', () => {
      expect(commitNumericDraft(null, 1200, NODE_BOUNDS)).toBeNull()
      expect(commitNumericDraft('abc', 1200, NODE_BOUNDS)).toBeNull()
      expect(commitNumericDraft('12.5', 1200, NODE_BOUNDS)).toBeNull()
    })

    it('commits a real edit, clamped to the published bound', () => {
      expect(commitNumericDraft('900', 1200, NODE_BOUNDS)).toBe(900)
      expect(commitNumericDraft('99999', 1200, NODE_BOUNDS)).toBe(5000)
      expect(commitNumericDraft('0', 1200, NODE_BOUNDS)).toBe(1)
    })

    it('discards a draft that equals the persisted value (no pointless PUT)', () => {
      expect(commitNumericDraft('1200', 1200, NODE_BOUNDS)).toBeNull()
    })
  })

  it('does not save when a numeric field is emptied and blurred', async () => {
    // The end-to-end shape of the same rule. Asserted after a real edit has been
    // committed, so the absence of a SECOND call is a settled fact rather than a
    // race with a mutation that may not have fired yet.
    await renderPanel()
    const input = await screen.findByLabelText('Max tree nodes')
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.blur(input)
    // Now make an edit that MUST save; once it lands, any empty-field save would
    // already have been recorded before it.
    fireEvent.change(input, { target: { value: '900' } })
    fireEvent.blur(input)
    await waitFor(() => expect(api.saveComputerUseConfig).toHaveBeenCalled())
    expect(api.saveComputerUseConfig).toHaveBeenCalledTimes(1)
    expect(api.saveComputerUseConfig).toHaveBeenCalledWith({ max_tree_nodes: 900 })
  })

  /* ── permission poll bound ──────────────────────────────────────────────── */

  describe('permissionPollInterval', () => {
    const t0 = 1_000_000

    it('stops for every state the backend documents as terminal', () => {
      // The panel used to re-poll while accessibility !== 'granted', which never
      // terminates on the shapes the backend itself returns as normal: `unknown`
      // (the probe could not run) and `unsupported` (no TCC on this platform).
      // Each tick shells out to a `kirocrew computer doctor --json` child.
      expect(permissionPollInterval('granted', t0, t0)).toBe(false)
      expect(permissionPollInterval('unknown', t0, t0)).toBe(false)
      expect(permissionPollInterval('unsupported', t0, t0)).toBe(false)
      // No data yet: the initial fetch is in flight, nothing to schedule against.
      expect(permissionPollInterval(undefined, t0, t0)).toBe(false)
    })

    it('polls while a grant is genuinely outstanding', () => {
      // The behaviour the poll was added for: granting in System Settings flips
      // the row without a reload.
      expect(permissionPollInterval('missing', t0, t0)).toBe(5000)
      expect(permissionPollInterval('missing', t0, t0 + 60_000)).toBe(5000)
    })

    it('stops once the bound elapses, even while still missing', () => {
      expect(permissionPollInterval('missing', t0, t0 + 180_000)).toBe(false)
      expect(permissionPollInterval('missing', t0, t0 + 600_000)).toBe(false)
    })

    it('treats a zero start time as "no deadline yet" rather than expired', () => {
      // `now - 0` is an enormous elapsed time, so without the guard an unset start
      // would read as already past the bound and kill the poll the row needs.
      expect(permissionPollInterval('missing', 0, t0)).toBe(5000)
    })
  })
  /* ── a malformed keystone ────────────────────────────────────────────────── */

  it('warns when the keystone app lists could not be read', async () => {
    // The backend deliberately still returns 200 for a hand-edited keystone: a 500
    // here would make the only UI that can repair the file unreachable. But the
    // lists come back EMPTY in that state, and an empty allow-list otherwise reads
    // as "no restriction configured" — the opposite of what the operator wrote.
    await renderPanel(snapshot({ policy_error: 'allowed_apps must be a JSON list of strings' }))
    expect(await screen.findByText(/could not be read/i)).toBeInTheDocument()
    // And it names the file, so the fix is actionable rather than a bare warning.
    expect(screen.getByText(/computer_use\.json/i)).toBeInTheDocument()
  })

  it('shows no warning when the policy parsed cleanly', async () => {
    // Inverse guard: every healthy install would otherwise carry a scary banner.
    await renderPanel()
    expect(screen.queryByText(/could not be read/i)).not.toBeInTheDocument()
  })
})
