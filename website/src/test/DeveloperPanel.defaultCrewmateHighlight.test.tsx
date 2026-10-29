/**
 * The default-crewmate deep link lands on Settings > Developer > Config.
 *
 * The Crewmates roster's `default` badge links to DEFAULT_CREWMATE_PATH, and the
 * Default crewmate row lives on the Developer panel's Config page. Settings
 * mounts `useSettingHighlight`, so the row must be ringed once the panel draws
 * it. The old `/developer?tab=config&highlight=…` form, kept alive by
 * DeveloperPage's redirect, must land on the same row.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

beforeEach(() => {
  // Not in jsdom; the highlight hook calls it on the target element.
  Element.prototype.scrollIntoView = vi.fn()
})

// The Config page renders the real KiroCrewCfgTab + AgentCfgTab. Only the Default
// crewmate ROW matters here, and the real tab needs config/roster queries this
// test does not mock — so stub the tab to render just that row, carrying the
// same `data-setting-key` anchor the real row does.
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => (
    <div data-testid="default-crewmate-row" data-setting-key="default-crewmate" data-setting-label="Default mate" />
  ),
  AgentCfgTab: () => <div />,
}))

// The sibling pages are heavy and never rendered here, but their static
// imports still load — stub the expensive ones.
vi.mock('../pages/overview/MemoryGraphTab', () => ({ default: () => <div /> }))
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))

import DeveloperPage from '../pages/DeveloperPage'
import { DeveloperPanel } from '../pages/settings/DeveloperPanel'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
import { DEFAULT_CREWMATE_PATH } from '../pages/overview/defaultCrewmateLink'

function Location() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname + loc.search}</div>
}

/** The two halves of SettingsPage this link depends on: the highlight hook
 *  and the Developer tab's panel. */
function SettingsHost() {
  useSettingHighlight()
  return <DeveloperPanel basePath="/settings" />
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/developer" element={<DeveloperPage />} />
        <Route path="/settings/*" element={<SettingsHost />} />
      </Routes>
      <Location />
    </MemoryRouter>,
  )
}

// Await the ConfigPage lazy import in DeveloperPanel.tsx before checking its row.
const LAZY_PAGE_TIMEOUT_MS = 5000

async function expectRinged() {
  const rowEl = await screen.findByTestId('default-crewmate-row', {}, { timeout: LAZY_PAGE_TIMEOUT_MS })
  // happy-dom mis-parses the `outline` shorthand with a var(), so assert on the
  // two distinct highlight props the hook also sets (see useSettingHighlight.test.ts).
  await waitFor(() => expect(rowEl.style.outlineOffset).toBe('4px'))
  expect(rowEl.style.borderRadius).toBe('8px')
  expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
}

describe('default-crewmate deep link', () => {
  it('points at the Config page of Settings > Developer', () => {
    expect(DEFAULT_CREWMATE_PATH).toBe('/settings/developer/config?highlight=key%3Adefault-crewmate')
  })

  it('rings the Default mate row', async () => {
    renderAt(DEFAULT_CREWMATE_PATH)
    await expectRinged()
  })

  it('rings the row from the old /developer link too', async () => {
    renderAt('/developer?tab=config&highlight=key:default-crewmate')
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toMatch(/^\/settings\/developer\/config\?/))
    await expectRinged()
  })
})
