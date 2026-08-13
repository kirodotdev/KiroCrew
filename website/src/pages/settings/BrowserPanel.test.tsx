import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these two methods, so mocking them
 * keeps every case network-free. The install POST resolves with the same shape
 * as the GET, which is what lets the panel re-render from one answer. */
vi.mock('../../api/client', () => ({
  api: {
    getBrowserInstall: vi.fn(),
    installBrowserCli: vi.fn(),
    installBrowserEngine: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { BrowserPanel } from './BrowserPanel'

type State = {
  installed: boolean
  cli_path: string | null
  cli_version: string | null
  node_ok: boolean
  node_version: string | null
  browser_ok: boolean
  installing: boolean
  last_error: string | null
  browsers?: Record<string, boolean>
}

function state(overrides: Partial<State> = {}): State {
  return {
    installed: false,
    cli_path: null,
    cli_version: null,
    node_ok: true,
    node_version: '22.0.0',
    browser_ok: false,
    installing: false,
    last_error: null,
    browsers: { chromium: true, firefox: false, webkit: false },
    ...overrides,
  }
}

async function renderPanel(data: State = state(), afterInstall: State = state({ installing: true })) {
  ;(api.getBrowserInstall as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  ;(api.installBrowserCli as ReturnType<typeof vi.fn>).mockResolvedValue(afterInstall)
  ;(api.installBrowserEngine as ReturnType<typeof vi.fn>).mockResolvedValue(afterInstall)
  const utils = renderWithProviders(<BrowserPanel />)
  // Wait for the SKELETON to go, not for the section title: the title renders
  // during loading too (deliberately -- it keeps the header from jumping when
  // data lands), so waiting on it would let every assertion below run against
  // the loading state.
  await waitFor(() => expect(document.querySelector('[data-slot="skeleton"]')).toBeNull())
  return utils
}

describe('BrowserPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reports browsing as available when the CLI is installed', async () => {
    await renderPanel(state({ installed: true, cli_version: '0.1.18', browser_ok: true }))
    expect(screen.getByText(/can browse web pages/i)).toBeTruthy()
    expect(screen.getByText(/0\.1\.18/)).toBeTruthy()
    // Presence-as-consent is disclosed here because no switch carries it.
    expect(screen.getByText(/installed. Removing it/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
  })

  it('offers the install when the CLI is absent', async () => {
    await renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Install Playwright CLI/i }))
    await waitFor(() => expect(api.installBrowserCli).toHaveBeenCalledTimes(1))
  })

  it('reports a too-old Node instead of an install button that would fail', async () => {
    await renderPanel(state({ node_ok: false, node_version: '18.4.0' }))
    expect(screen.getByText(/Needs Node\.js 20 or newer/i)).toBeTruthy()
    expect(screen.getByText(/18\.4\.0/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
  })

  it('shows progress for an install already running on the gateway', async () => {
    await renderPanel(state({ installing: true }))
    // A second click cannot start a duplicate npm run.
    const button = screen.getByRole('button', { name: /Installing/i }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(screen.getByText(/downloads a browser/i)).toBeTruthy()
  })

  it('surfaces a failed install verbatim rather than a success tick', async () => {
    await renderPanel(state({ last_error: 'npm: E401 Unable to authenticate' }))
    expect(screen.getByText(/E401 Unable to authenticate/)).toBeTruthy()
  })

  it('offers the failed install to the agent, so an npm error is not a dead end', async () => {
    await renderPanel(state({ last_error: 'npm-install-global: npm error code EACCES' }))
    expect(screen.getByText(/EACCES/)).toBeTruthy()
    // The repo's AskAgentButton renders with this title; its presence is what
    // makes the error actionable for a user who cannot fix npm themselves.
    expect(screen.getByTitle(/open a chat with this error/i)).toBeTruthy()
  })

  it('disables every engine row while one download is pending', async () => {
    // The gateway has one install slot and `data.installing` only flips on the
    // next poll, so without engineMut.isPending a second click lands in that
    // window and the spinner follows the WRONG engine.
    await renderPanel(
      state({
        installed: true,
        cli_version: '0.1.18',
        browser_ok: true,
        browsers: { chromium: true, firefox: false, webkit: false },
      }),
    )
    // Hold the mutation open: `isPending` is the whole subject, and a mock that
    // resolves immediately closes the window before any assertion can see it.
    ;(api.installBrowserEngine as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise(() => {}),
    )
    const firefox = screen.getByTestId('browser-engine-firefox')
    fireEvent.click(firefox.querySelector('button') as HTMLButtonElement)
    await waitFor(() =>
      expect(
        (screen.getByTestId('browser-engine-webkit').querySelector('button') as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
  })

  it('names the remedy when Node is missing entirely, not just the requirement', async () => {
    // The blocked state used to interpolate the found version into one sentence,
    // producing "found none" and telling a first-time user nothing about what to
    // do. Absent and too-old are different problems with the same fix, and the
    // fix has to be reachable from the panel.
    await renderPanel(state({ node_ok: false, node_version: null }))
    expect(screen.getByText(/no Node was found/i)).toBeTruthy()
    // The label is a catalog-sourced action, not a bare domain: the i18n render
    // gate rejects hardcoded Latin text and DNT rejects respelling Node.js.
    expect(screen.getByRole('link', { name: /Download Node\.js/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Install Playwright CLI/i })).toBeNull()
  })

  it('offers every engine as its own download, not just the one attach needs', async () => {
    // The old panel reported ONE browser boolean, which made Firefox and WebKit
    // look unavailable. Each engine is a separate download and gets its own row.
    await renderPanel(
      state({
        installed: true,
        cli_version: '0.1.18',
        browser_ok: true,
        browsers: { chromium: true, firefox: false, webkit: false },
      }),
    )
    expect(screen.getByTestId('browser-engine-chromium')).toBeTruthy()
    expect(screen.getByTestId('browser-engine-firefox')).toBeTruthy()
    expect(screen.getByTestId('browser-engine-webkit')).toBeTruthy()

    // A downloaded engine shows state, not a button; a missing one is actionable.
    // Asserted BEFORE the click: the mutation's onSuccess replaces the cached
    // status with the mock's answer, so anything checked afterwards is a
    // different render.
    expect(screen.getByTestId('browser-engine-chromium').querySelector('button')).toBeNull()
    const firefox = screen.getByTestId('browser-engine-firefox')
    fireEvent.click(firefox.querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(api.installBrowserEngine).toHaveBeenCalledWith('firefox'))
  })
})
