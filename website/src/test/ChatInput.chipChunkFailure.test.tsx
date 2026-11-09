/**
 * A failed chunk load of a lazy composer chip stays in the composer.
 *
 * The goal and monitor control and the session environment chip each load in
 * their own chunk, and the main composer mounts both on every chat view with an
 * active session. When a chip's chunk fails to load, the failure must show in
 * the chip's own place: the sessions sidebar, the chat page around the
 * composer and the message box stay mounted. Try Again must run the import
 * again, because React.lazy caches a rejected import, and Reload page is the
 * way back for a browser that keeps failing the same chunk. The failure is one
 * line in its row with no agent hand-off, because the unsaved draft sits beside
 * it.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import ChatInput from '../components/ChatInput'
import ErrorBoundary from '../components/ErrorBoundary'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { renderWithProviders } from './helpers'

const chunks = vi.hoisted(() => ({
  environment: { fail: true, imports: 0 },
  automation: { fail: true, imports: 0 },
}))

// Each chip's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
vi.mock('../components/SessionEnvironmentChip', () => {
  chunks.environment.imports += 1
  if (chunks.environment.fail) throw new TypeError('Importing a module script failed.')
  return { default: () => <div data-testid="environment-chip-loaded" /> }
})
vi.mock('../components/SessionAutomationPopover', () => {
  chunks.automation.imports += 1
  if (chunks.automation.fail) throw new TypeError('Importing a module script failed.')
  return { default: () => <div data-testid="automation-chip-loaded" /> }
})

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

const tryAgainName = () => i18nT('components.errorBoundary.try_again')
const reloadName = () => i18nT('components.errorBoundary.reload_page')

/**
 * Waits until both chips' failures are drawn. Each comes from its own
 * import, so one chip's Try Again does not settle the other chip's place.
 */
const chipFailuresReady = () => waitFor(
  () => expect(screen.getAllByRole('button', { name: tryAgainName() })).toHaveLength(2),
  { timeout: CHUNK_SETTLE_TIMEOUT_MS },
)

afterEach(() => { vi.restoreAllMocks() })

describe('ChatInput when a lazy chip chunk fails to load', () => {
  it('keeps each failure in its chip\'s place in the composer, and Try Again imports the chunk again', async () => {
    // React logs the caught render errors; the boundaries handling them are the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [] })
    renderWithProviders(
      // The chat route's own boundary (App.tsx), standing in for "the whole
      // chat page was replaced".
      <ErrorBoundary fallback={<div data-testid="chat-route-crashed" />}>
        <aside data-testid="sessions-sidebar" />
        <ChatInput value="my draft" onChange={vi.fn()} onSend={vi.fn()}
          environmentSlot="chat-1" onAutomationClick={vi.fn()} />
      </ErrorBoundary>,
    )
    // A timeout falls through to the checks below, which name the place that
    // went wrong (a failure that escaped to the chat route draws no Try Again).
    await chipFailuresReady().catch(() => {})
    const retries = screen.queryAllByRole('button', { name: tryAgainName() })
    expect(chunks.environment.imports).toBe(1)
    expect(chunks.automation.imports).toBe(1)

    expect.soft(screen.queryByTestId('chat-route-crashed'), 'the failure replaced the whole chat page').toBeNull()
    expect.soft(screen.queryByTestId('sessions-sidebar'), 'the sessions sidebar survives').toBeInTheDocument()
    expect.soft(
      screen.queryByRole('textbox', { name: i18nT('components.chatInput.message_input') }),
      'the message box and its draft survive',
    ).toHaveValue('my draft')

    const shelf = screen.queryByTestId('composer-context-shelf')
    const controlRow = screen.queryByTestId('composer-control-row')
    const places = [
      { chip: 'environment', place: shelf, title: i18nT('components.chatInput.environment_load_failed'), loaded: 'environment-chip-loaded' },
      { chip: 'automation', place: controlRow, title: i18nT('components.chatInput.automation_load_failed'), loaded: 'automation-chip-loaded' },
    ] as const
    for (const { chip, place, title } of places) {
      expect.soft(place, `the composer still draws the ${chip} chip's place`).not.toBeNull()
      if (!place) continue
      expect.soft(within(place).queryByText(title), `the ${chip} failure names what could not load, in the chip's place`).toBeInTheDocument()
      expect.soft(within(place).queryByRole('button', { name: reloadName() }), `the ${chip} failure offers Reload page`).toBeInTheDocument()
      // A hand-off opens a new chat, which would leave the unsaved draft beside it.
      expect.soft(
        within(place).queryByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }),
        `the ${chip} failure offers no agent hand-off beside the unsaved draft`,
      ).toBeNull()
      // One line in the row: the raw chunk error is read out to assistive tech,
      // not drawn beside the title, so the failure never grows the row.
      const notice = within(place).queryByText(title)?.parentElement
      const readOut = Array.from(notice?.children ?? []).find(child => child.classList.contains('sr-only'))
      expect.soft(readOut?.textContent, `the ${chip} failure keeps the raw chunk error off the row`).toBeTruthy()
    }
    expect(retries, 'each failure offers Try Again').toHaveLength(2)

    const reload = within(shelf!).getByRole('button', { name: reloadName() })
    const original = window.location
    const reloadSpy = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload: reloadSpy } })
    try {
      fireEvent.click(reload)
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
    expect(reloadSpy).toHaveBeenCalledTimes(1)

    for (const { chip, place, loaded } of places) {
      chunks[chip].fail = false
      fireEvent.click(within(place!).getByRole('button', { name: tryAgainName() }))
      expect(
        await within(place!).findByTestId(loaded, {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
        `Try Again mounts the ${chip} chip once its chunk loads`,
      ).toBeInTheDocument()
      expect(chunks[chip].imports, `Try Again runs the ${chip} import again`).toBe(2)
    }
  })
})
