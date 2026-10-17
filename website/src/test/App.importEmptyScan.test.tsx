/**
 * The Import chapter, replayed by hand, on a machine with nothing to import.
 *
 * First run ends the chapter by itself on an empty scan: nobody asked for
 * anything, and the next chapter is waiting. "Import from another agent" in
 * Settings is different. It dispatches `mc-start-import`, and the same
 * auto-finish closed the dialog before it painted, so the click was answered by
 * nothing at all.
 *
 * Settings replays the importer alone and keeps the empty result on screen.
 * `/onboarding` replays all of first run (its event adds `continueOnboarding`),
 * and an empty import step there still passes by itself, as it does at first run.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import type { AgentImportScanResponse } from '../api/client'

/** Mutable so a test can pick which first-run flags the shell boots with. */
const themeState = {
  colorTheme: 'kiro',
  theme: 'dark' as const,
  mode: 'dark' as const,
  onboarded: true,
  importOnboarded: true,
  privacyAcked: true,
  crewmatesOnboarded: true,
  crewmatesFlowSeen: true,
  themeBootReady: true,
  themes: [],
  markOnboarded: vi.fn(),
  markImportOnboarded: vi.fn(),
  markPrivacyAcked: vi.fn(),
  markCrewmatesOnboarded: vi.fn(),
  setColorTheme: vi.fn(),
  setMode: vi.fn(),
}

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => themeState,
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))

import { api } from '../api/client'
import { renderWithProviders } from './helpers'
import App from '../App'

const NOTHING_TO_IMPORT: AgentImportScanResponse = {
  sources: [{ id: 'codex', name: 'Codex', detected: false, categories: [] }],
  skipped: [],
  merge_only: true,
}

const EMPTY_RESULT = { name: 'No supported setup found' }

// Every wait below spans several async hops (the scan query, then the auto-complete effect or
// the Skip click, then the completion mutation and its `onSuccess`). The 1,000 ms default is a
// bet on runner speed; this ceiling only turns a lost run into a failure that names itself.
const CHAIN_WAIT = { timeout: 5000 }

beforeEach(() => {
  themeState.onboarded = true
  themeState.importOnboarded = true
  themeState.privacyAcked = true
  themeState.markImportOnboarded.mockClear()
  vi.spyOn(api, 'onboardingImportScan').mockResolvedValue(NOTHING_TO_IMPORT)
  vi.spyOn(api, 'onboardingImportState').mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Import chapter on an empty scan', () => {
  it('answers a replay from Settings with the empty result', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    act(() => { window.dispatchEvent(new Event('mc-start-import')) })

    // Chain: the replay event -> the dialog mounts -> the scan query -> the empty result.
    expect(await screen.findByRole('heading', EMPTY_RESULT, CHAIN_WAIT)).toBeInTheDocument()
    expect(api.onboardingImportState).not.toHaveBeenCalled()
    expect(themeState.markImportOnboarded).not.toHaveBeenCalled()

    // The dialog is alive: its own exit finishes the chapter, once.
    // Chain: the Skip click -> the completion mutation -> its `onSuccess` -> `markImportOnboarded`.
    await userEvent.click(screen.getByRole('button', { name: 'Skip import' }))
    await waitFor(() => expect(themeState.markImportOnboarded).toHaveBeenCalledOnce(), CHAIN_WAIT)
    // Same chain, one hop further: the finished chapter closes the dialog.
    await waitFor(
      () => expect(screen.queryByRole('heading', EMPTY_RESULT)).not.toBeInTheDocument(),
      CHAIN_WAIT,
    )
    expect(api.onboardingImportState).toHaveBeenCalledOnce()
  })

  it('answers a second replay the same way', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    act(() => { window.dispatchEvent(new Event('mc-start-import')) })
    // Chain: the replay event -> the dialog mounts -> the scan query -> the empty result's button.
    await userEvent.click(await screen.findByRole('button', { name: 'Skip import' }, CHAIN_WAIT))
    // Chain: the Skip click -> the completion mutation -> the dialog closes.
    await waitFor(
      () => expect(screen.queryByRole('heading', EMPTY_RESULT)).not.toBeInTheDocument(),
      CHAIN_WAIT,
    )

    act(() => { window.dispatchEvent(new Event('mc-start-import')) })

    // Chain: the second replay event -> the dialog mounts again -> the scan query -> the result.
    expect(await screen.findByRole('heading', EMPTY_RESULT, CHAIN_WAIT)).toBeInTheDocument()
    expect(api.onboardingImportScan).toHaveBeenCalledTimes(2)
  })

  it('lets /onboarding pass an empty import step by itself', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }))
    })

    // Chain: the scan query -> the auto-complete effect -> the completion mutation -> `onSuccess`.
    await waitFor(() => expect(themeState.markImportOnboarded).toHaveBeenCalledOnce(), CHAIN_WAIT)
    expect(api.onboardingImportState).toHaveBeenCalledWith({ completed: true })
    expect(screen.queryByRole('heading', EMPTY_RESULT)).not.toBeInTheDocument()
  })

  it('lets first run finish its own import chapter by itself', async () => {
    themeState.importOnboarded = false
    renderWithProviders(<App />, { route: '/chat' })

    // Chain: the scan query -> the auto-complete effect -> the completion mutation -> `onSuccess`.
    await waitFor(() => expect(themeState.markImportOnboarded).toHaveBeenCalledOnce(), CHAIN_WAIT)
    expect(api.onboardingImportState).toHaveBeenCalledWith({ completed: true })
    expect(screen.queryByRole('heading', EMPTY_RESULT)).not.toBeInTheDocument()
  })
})
