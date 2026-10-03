import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders } from '../test/helpers'
import { hasNoCrewmates, useMeetCrewmatesGate } from './useMeetCrewmatesGate'
import { useTheme } from './useTheme'
import { CREWMATES_PAGE_ENTERED_EVENT, START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { api } from '../api/client'

// A brand-new workspace: first-run chapters not yet done on the server, only
// the built-in `default` row on the roster, only the built-in agent installed.
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      themeBoot: vi.fn().mockResolvedValue({
        mode: '', color: '', onboarded: false, import_onboarded: true, privacy_acked: true,
      }),
      updateThemeConfig: vi.fn().mockResolvedValue({}),
      members: vi.fn().mockResolvedValue({ members: [{ name: 'default', slug: 'default' }] }),
      agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew' }]),
    },
  }
})

const useBoth = () => ({ gate: useMeetCrewmatesGate(), theme: useTheme() })

/** How many `PUT /api/config/theme` calls carried `crewmates_onboarded`
 *  (the tour's own `onboarded` write is not one of them). */
const crewmateWrites = () =>
  vi.mocked(api.updateThemeConfig).mock.calls.filter(
    c => (c[0] as { crewmates_onboarded?: boolean } | undefined)?.crewmates_onboarded === true,
  ).length

const start = () => act(() => { window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT)) })
const enterPage = () => act(() => { window.dispatchEvent(new Event(CREWMATES_PAGE_ENTERED_EVENT)) })
const settle = () => new Promise(r => setTimeout(r, 20))

describe('hasNoCrewmates', () => {
  it('treats a default-only roster as empty; the built-in Assistant member is a crewmate', () => {
    expect(hasNoCrewmates([])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'assistant' }])).toBe(false)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'Radar' }])).toBe(false)
    expect(hasNoCrewmates(undefined)).toBe(false)
  })
})

describe('useMeetCrewmatesGate', () => {
  beforeEach(() => {
    localStorage.clear()
    // The Crew Members preview used to be the tour-end trigger; it is on here
    // so the "never auto-opens" cases below are not passing for that reason.
    localStorage.setItem(PREVIEW_CREW, '1')
    vi.mocked(api.updateThemeConfig).mockReset()
    vi.mocked(api.updateThemeConfig).mockResolvedValue({} as never)
    vi.mocked(api.members).mockReset()
    vi.mocked(api.agentsInstalled).mockReset()
    vi.mocked(api.members).mockResolvedValue({ members: [{ name: 'default', slug: 'default' }] } as never)
    vi.mocked(api.agentsInstalled).mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew', kirocrew_owned: true }] as never)
  })

  it('never auto-opens at the end of the first-run tour', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await settle()
    expect(result.current.gate.open).toBe(false)
    // The gate reads neither the roster nor the installed agents.
    expect(api.members).not.toHaveBeenCalled()
    expect(api.agentsInstalled).not.toHaveBeenCalled()
  })

  it('never auto-opens on entering the Crewmates page, even for a workspace that never saw it', async () => {
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
    })
    const { result } = renderHookWithProviders(useBoth)
    enterPage()
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    enterPage()
    await settle()
    expect(result.current.gate.open).toBe(false)
  })

  it('opens only on the explicit start event, and again on each later request', async () => {
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
      crewmates_onboarded: true,
    } as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.gate.open).toBe(false)
    start()
    expect(result.current.gate.open).toBe(true)
    act(() => result.current.gate.onDone('dismissed'))
    expect(result.current.gate.open).toBe(false)
    start()
    expect(result.current.gate.open).toBe(true)
  })

  it('stays open through onCreated and closes only on onDone; completion is recorded', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    start()
    act(() => result.current.gate.onCreated())
    await waitFor(() => expect(crewmateWrites()).toBe(1))
    expect(result.current.gate.open).toBe(true)
    // onDone closes at once, before any write resolves.
    act(() => result.current.gate.onDone('completed'))
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(crewmateWrites()).toBe(2))
    expect(result.current.gate.persistFailed).toBe(false)
  })

  it('a dismissal closes and writes nothing', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    start()
    act(() => result.current.gate.onDone('dismissed'))
    expect(result.current.gate.open).toBe(false)
    await settle()
    expect(crewmateWrites()).toBe(0)
  })

  it('a persist refused at onCreated is shown while open, carried to the next entry, and cleared by a later success; nothing local is marked', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    start()
    vi.mocked(api.updateThemeConfig).mockRejectedValue(new Error('refused'))
    act(() => result.current.gate.onCreated())
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(true))
    expect(result.current.gate.open).toBe(true)
    // Exit closes at once even though its own write is refused too.
    act(() => result.current.gate.onDone('completed'))
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(crewmateWrites()).toBe(2))
    expect(localStorage.getItem('mc-crewmates-onboarded')).toBeNull()
    expect(result.current.theme.crewmatesOnboarded).toBe(false)
    // The next explicit entry still carries the notice...
    start()
    expect(result.current.gate.persistFailed).toBe(true)
    // ...until a write succeeds.
    vi.mocked(api.updateThemeConfig).mockResolvedValue({} as never)
    act(() => result.current.gate.onCreated())
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(false))
  })

  it('open and persistFailed are independent state: a refusal never opens or closes the flow', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    vi.mocked(api.updateThemeConfig).mockRejectedValue(new Error('refused'))
    act(() => result.current.gate.onCreated())
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(true))
    expect(result.current.gate.open).toBe(false)
  })
})
