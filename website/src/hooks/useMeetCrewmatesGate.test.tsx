import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders } from '../test/helpers'
import { useMeetCrewmatesGate } from './useMeetCrewmatesGate'
import { useTheme } from './useTheme'
import { START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
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

describe('useMeetCrewmatesGate', () => {
  beforeEach(() => {
    localStorage.clear()
    // The Crew Members preview is the launch switch; every case below opts in.
    localStorage.setItem(PREVIEW_CREW, '1')
    // Reset, not clear: a case that installs a persistent rejection must not
    // leak it into the next one (a refused PUT now leaves the pending mark).
    vi.mocked(api.updateThemeConfig).mockReset()
    vi.mocked(api.updateThemeConfig).mockResolvedValue({} as never)
    vi.mocked(api.members).mockReset()
    vi.mocked(api.agentsInstalled).mockReset()
    vi.mocked(api.members).mockResolvedValue({ members: [{ name: 'default', slug: 'default' }] } as never)
    vi.mocked(api.agentsInstalled).mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew', kirocrew_owned: true }] as never)
  })

  it('stays closed until the tour ends, then opens, stays open through onCreated, and closes only on onDone', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.gate.open).toBe(false)

    // The Customize tour finishes in this session.
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))

    // The crewmate exists: "done" is persisted, but the ready step is still up.
    act(() => result.current.gate.onCreated())
    await waitFor(() =>
      expect(api.updateThemeConfig).toHaveBeenCalledWith({ crewmates_onboarded: true }),
    )
    expect(result.current.gate.open).toBe(true)

    // onDone closes at once -- several exits navigate right after it, and the
    // chapter must not cover the page they go to for a round trip.
    act(() => result.current.gate.onDone('completed'))
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(crewmateWrites()).toBe(2))
    expect(result.current.gate.persistFailed).toBe(false)
  })

  it('a persist refused at exit still closes at once; the refusal is shown on the next entry, until a write succeeds', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))
    // Armed only now: markOnboarded's own PUT above must not consume it.
    vi.mocked(api.updateThemeConfig).mockRejectedValueOnce(new Error('500'))
    act(() => result.current.gate.onDone('dismissed'))
    // The exit did not wait for the server.
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(true))
    // Nothing local was marked done on the refusal.
    expect(localStorage.getItem('mc-crewmates-onboarded')).toBeNull()
    expect(result.current.theme.crewmatesOnboarded).toBe(false)
    // The next entry carries the notice...
    act(() => {
      window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT))
    })
    expect(result.current.gate.open).toBe(true)
    expect(result.current.gate.persistFailed).toBe(true)
    // ...and a write that succeeds clears it.
    act(() => result.current.gate.onDone('dismissed'))
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(false))
  })

  it('does not open while the Crew Members preview is off', async () => {
    localStorage.removeItem(PREVIEW_CREW)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    // Neither gating read is made: the switch is off, so nothing is eligible.
    expect(api.members).not.toHaveBeenCalled()
    expect(result.current.gate.open).toBe(false)
  })

  it('does not open when a crewmate already exists', async () => {
    vi.mocked(api.members).mockResolvedValue({
      members: [{ name: 'default', slug: 'default' }, { name: 'Radar', slug: 'radar' }],
    } as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(api.members).toHaveBeenCalled())
    expect(result.current.gate.open).toBe(false)
  })

  it('a persist refused at onCreated is shown on the ready step; onDone closes at once and marks nothing locally', async () => {
    vi.mocked(api.updateThemeConfig).mockRejectedValue(new Error('refused'))
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))
    act(() => result.current.gate.onCreated())
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(true))
    expect(result.current.gate.open).toBe(true)
    // Open <Name>'s chat -> onDone: the write fails again; the exit closed anyway,
    // before the server answered, so the chat page is not covered by the chapter.
    act(() => result.current.gate.onDone('completed'))
    expect(result.current.gate.open).toBe(false)
    await waitFor(() => expect(crewmateWrites()).toBe(2))
    // Nothing local was marked done on a refusal: the render cache is unset,
    // the tour's pending mark is intact, the in-memory flag is still false --
    // so a reload re-offers the chapter (what the notice promised) instead of
    // seeding a completion the server never recorded.
    expect(localStorage.getItem('mc-crewmates-onboarded')).toBeNull()
    expect(localStorage.getItem('mc-crewmates-pending')).toBe('1')
    expect(result.current.theme.crewmatesOnboarded).toBe(false)
    // ...but it does not auto-fire again in THIS session (the user answered it once).
    await new Promise(r => setTimeout(r, 20))
    expect(result.current.gate.open).toBe(false)
    act(() => {
      window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT))
    })
    expect(result.current.gate.open).toBe(true)
  })

  it('a failed eligibility read is surfaced, not swallowed: eligibilityError until dismissed, flow stays closed', async () => {
    // React Query retries by default; the providers' test client disables retries, so one rejection is the failure.
    vi.mocked(api.members).mockRejectedValue(new Error('network'))
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.gate.eligibilityError).toBe(false)
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.eligibilityError).toBe(true))
    expect(result.current.gate.open).toBe(false)
    act(() => result.current.gate.dismissEligibilityError())
    expect(result.current.gate.eligibilityError).toBe(false)
    // The Crew Members page entry still works regardless of the failed read.
    act(() => window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT)))
    expect(result.current.gate.open).toBe(true)
  })

  it('a spec owned by Kiro Crew that is not in the name fallback still counts as built-in (server flag wins)', async () => {
    vi.mocked(api.agentsInstalled).mockResolvedValue([
      { name: 'kirocrew', kirocrew_owned: true },
      { name: 'kirocrew-some-future-builtin', kirocrew_owned: true },
    ] as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))
  })

  it('a user spec with the server flag false is a custom agent even if its name looks built-in', async () => {
    vi.mocked(api.agentsInstalled).mockResolvedValue([
      { name: 'kirocrew', kirocrew_owned: true },
      { name: 'kirocrew-lite', kirocrew_owned: false },
    ] as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(api.agentsInstalled).toHaveBeenCalled())
    expect(result.current.gate.open).toBe(false)
  })

  it('does not open when a custom agent is installed (that user gets the opt-in step)', async () => {
    vi.mocked(api.agentsInstalled).mockResolvedValue([{ name: 'kirocrew', kirocrew_owned: true }, { name: 'issue-triage', kirocrew_owned: false }] as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(api.agentsInstalled).toHaveBeenCalled())
    expect(result.current.gate.open).toBe(false)
  })

  it('a NEW user who reloads between the tour and this chapter is still due it: the tour leaves a pending mark', async () => {
    // First page load: the tour finishes here (markOnboarded), the chapter opens.
    const first = renderHookWithProviders(useBoth)
    await waitFor(() => expect(first.result.current.theme.themeBootReady).toBe(true))
    act(() => first.result.current.theme.markOnboarded())
    await waitFor(() => expect(first.result.current.gate.open).toBe(true))
    expect(localStorage.getItem('mc-crewmates-pending')).toBe('1')
    first.unmount()

    // Reload: the server now says onboarded, the browser still holds the mark.
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
    })
    const second = renderHookWithProviders(useBoth)
    await waitFor(() => expect(second.result.current.theme.themeBootReady).toBe(true))
    expect(second.result.current.theme.crewmatesOnboarded).toBe(false)
    await waitFor(() => expect(second.result.current.gate.open).toBe(true))

    // Finishing the chapter clears the mark, so it cannot outlive its purpose.
    act(() => second.result.current.gate.onCreated())
    await waitFor(() => expect(localStorage.getItem('mc-crewmates-pending')).toBeNull())
    expect(localStorage.getItem('mc-crewmates-onboarded')).toBe('1')
  })

  it('an existing user replaying the tour leaves no pending mark: the next reload does not auto-open the chapter', async () => {
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
    })
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.theme.onboarded).toBe(true)
    // Replaying the tour from Settings ends in markOnboarded again.
    act(() => result.current.theme.markOnboarded())
    expect(localStorage.getItem('mc-crewmates-pending')).toBeNull()
    expect(result.current.gate.open).toBe(false)
  })

  it('a workspace already onboarded on the server is treated as done, but the Crewmates page can still open it', async () => {
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
    })
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.theme.crewmatesOnboarded).toBe(true)
    expect(result.current.gate.open).toBe(false)
    act(() => {
      window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT))
    })
    expect(result.current.gate.open).toBe(true)
  })
})
