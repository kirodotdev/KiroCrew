/**
 * The What's-new modal's update switches, rendered on their own: which rows a
 * window gets (`utils/updateSwitches`) and how each one reads and writes. The
 * App-shell half (the modal mounting them, the hand-off closing it, the read
 * on open) is `test/App.changelogModalApply.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../test/helpers'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'
import { updateInfoQuery } from '../api/updateInfoQuery'
import WhatsNewAutoUpdateToggle from './WhatsNewAutoUpdateToggle'

const { kirocrewConfig, setAutoUpdate } = vi.hoisted(() => ({
  kirocrewConfig: vi.fn(),
  setAutoUpdate: vi.fn(),
}))
vi.mock('../api/client', async importOriginal => {
  const real = await importOriginal<typeof import('../api/client')>()
  return { ...real, api: { ...real.api, kirocrewConfig, setAutoUpdate } }
})

const GATEWAY = i18nT('pages.settings.aboutPanel.automatic_updates')
const APP = i18nT('pages.settings.aboutPanel.install_app_updates_automatically')

const withEffect = (update_auto_effect?: string, over: Record<string, unknown> = {}) => ({
  dashboard: {
    status: { uptime: '1m', sessions: 0, messages: 0, update_auto_effect, ...over },
  } as unknown as RootState['dashboard'],
})

const shell = (over: Partial<UpdateAPI> = {}): UpdateAPI => ({
  onState: () => () => {},
  check: vi.fn().mockResolvedValue({ ok: true }),
  install: vi.fn().mockResolvedValue({ ok: true }),
  getInfo: vi.fn().mockResolvedValue({ autoDownload: false }),
  setAutoDownload: vi.fn().mockResolvedValue({ ok: true }),
  ...over,
})

const show = (effect?: string, over: Record<string, unknown> = {}) =>
  renderWithProviders(<WhatsNewAutoUpdateToggle onHandoff={vi.fn()} />, { store: createTestStore(withEffect(effect, over)) })

const live = (name: string) => expect(screen.getByRole('switch', { name })).not.toHaveAttribute('aria-disabled')

beforeEach(() => {
  kirocrewConfig.mockReset()
  kirocrewConfig.mockResolvedValue({ auto_update: true })
  setAutoUpdate.mockReset()
  setAutoUpdate.mockImplementation(async (enabled: boolean) => ({ ok: true, auto_update: enabled }))
})
afterEach(() => {
  delete window.updateAPI
})

// The rule's case table is `utils/updateSwitches.test.ts`; these only prove
// the modal feeds it this window's bridge, the updater's answer and the effect.
describe('which switches a window gets', () => {
  it('offers both in a desktop window attached to a gateway that installs', async () => {
    // A reused cli.sh service or an SSH-forwarded gateway installs on its own switch.
    window.updateAPI = shell()
    show('install')
    await waitFor(() => { live(APP); live(GATEWAY) })
  })

  it('draws nothing once the updater reports itself disabled and the gateway cannot install', async () => {
    window.updateAPI = shell({ getInfo: vi.fn().mockResolvedValue({ disabled: 'dev' }) })
    const { queryClient } = show('notify')
    await waitFor(() => expect(queryClient.getQueryState(updateInfoQuery.queryKey)?.status).toBe('success'))
    expect(screen.queryByTestId('whats-new-update-switches')).toBeNull()
  })

  it("says so where an operator's update policy owns the gateway's updates", async () => {
    show('install', { update_managed_by: 'command' })
    expect(screen.getByText(i18nT('pages.settings.aboutPanel.updates_managed_by_policy'))).toBeTruthy()
    // The switch still governs whether the policy's apply command runs unattended.
    await waitFor(() => live(GATEWAY))
  })

  it('carries no highlight anchors, so a Settings deep link lands on About', async () => {
    show('install')
    await waitFor(() => live(GATEWAY))
    expect(document.querySelector('[data-setting-key], [data-setting-label]')).toBeNull()
  })
})

describe('reading and writing', () => {
  it("reports a failed read of the app updater's state", async () => {
    window.updateAPI = shell({ getInfo: vi.fn().mockRejectedValue(new Error('zzq')) })
    show('notify')
    expect(await screen.findByText(i18nT('pages.settings.aboutPanel.auto_download_unreadable'))).toBeTruthy()
    // Still the way to opt out, at the shell's default.
    live(APP)
    expect(screen.getByRole('switch', { name: APP })).toHaveAttribute('aria-checked', 'true')
  })

  it("drives the app updater's preference, not the gateway config", async () => {
    const bridge = shell()
    window.updateAPI = bridge
    show('notify')
    await waitFor(() => live(APP))
    fireEvent.click(screen.getByRole('switch', { name: APP }))
    await waitFor(() => expect(bridge.setAutoDownload).toHaveBeenCalledWith(true))
    expect(setAutoUpdate).not.toHaveBeenCalled()
  })

  it('shows the gateway’s refusal next to the switch', async () => {
    setAutoUpdate.mockRejectedValueOnce(new Error('zzq save refused'))
    show('install')
    await waitFor(() => live(GATEWAY))
    fireEvent.click(screen.getByRole('switch', { name: GATEWAY }))
    expect(await screen.findByText('zzq save refused')).toBeTruthy()
    expect(screen.getByRole('switch', { name: GATEWAY })).toHaveAttribute('aria-checked', 'true')
  })

  it('sends the saves one at a time, in click order', async () => {
    // The gateway keeps whichever write ARRIVES last, so a second click must not
    // race the first: off, then back on, has to end on.
    const answers: Array<() => void> = []
    setAutoUpdate.mockImplementation((enabled: boolean) => new Promise(resolve => {
      answers.push(() => resolve({ ok: true, auto_update: enabled }))
    }))
    const { queryClient } = show('install')
    await waitFor(() => live(GATEWAY))
    const toggle = screen.getByRole('switch', { name: GATEWAY })

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(setAutoUpdate.mock.calls).toEqual([[false]])

    kirocrewConfig.mockResolvedValue({ auto_update: false })
    await act(async () => { answers[0]() })
    await waitFor(() => expect(setAutoUpdate.mock.calls).toEqual([[false], [true]]))
    kirocrewConfig.mockResolvedValue({ auto_update: true })
    await act(async () => { answers[1]() })

    await waitFor(() => expect((queryClient.getQueryData(['kirocrewConfig']) as { auto_update?: boolean }).auto_update).toBe(true))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('reads the saved value each time it opens', async () => {
    const { rerender } = show('install')
    await waitFor(() => live(GATEWAY))
    expect(kirocrewConfig).toHaveBeenCalledTimes(1)
    // Closed, then opened again on the same cache: the cached value is fresh,
    // and is read again anyway.
    rerender(<></>)
    kirocrewConfig.mockResolvedValue({ auto_update: false })
    rerender(<WhatsNewAutoUpdateToggle onHandoff={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('switch', { name: GATEWAY })).toHaveAttribute('aria-checked', 'false'))
    expect(kirocrewConfig).toHaveBeenCalledTimes(2)
  })
})
