/**
 * DeveloperPage serves the default-crewmate deep link.
 *
 * The Crewmates roster's `default` badge links to
 * `/developer?tab=config&highlight=key:default-crewmate` (DEFAULT_CREWMATE_PATH),
 * and the Default crewmate row lives on this page's Config tab. So DeveloperPage
 * must mount `useSettingHighlight` for that route+tab, or the badge lands on the
 * Config pane with nothing ringed. The hook itself is unit-tested; this proves
 * DeveloperPage actually mounts it (gated off the legacy agent-backend tab, whose
 * highlight it forwards to Settings instead).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

beforeEach(() => {
  // Not in jsdom; the highlight hook calls it on the target element.
  Element.prototype.scrollIntoView = vi.fn()
})

// The Config tab renders the real KiroCrewCfgTab + AgentCfgTab. Only the Default
// crewmate ROW matters here, and the real tab needs config/roster queries this
// test does not mock — so stub the tab to render just that row, carrying the
// same `data-setting-key` anchor the real row does.
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => (
    <div data-testid="default-crewmate-row" data-setting-key="default-crewmate" data-setting-label="Default crewmate" />
  ),
  AgentCfgTab: () => <div />,
}))

// The sibling tabs are heavy and never rendered here (only the Config tab is
// active), but their static imports still load — stub the expensive ones.
vi.mock('../pages/overview/MemoryGraphTab', () => ({ default: () => <div /> }))
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))

import DeveloperPage from '../pages/DeveloperPage'

describe('DeveloperPage default-crewmate deep link', () => {
  it('rings the Default crewmate row when the config tab carries its highlight', async () => {
    const { getByTestId } = render(
      <MemoryRouter initialEntries={['/developer?tab=config&highlight=key:default-crewmate']}>
        <DeveloperPage />
      </MemoryRouter>,
    )
    const rowEl = getByTestId('default-crewmate-row')
    // happy-dom mis-parses the `outline` shorthand with a var(), so assert on the
    // two distinct highlight props the hook also sets (see useSettingHighlight.test.ts).
    await waitFor(() => expect(rowEl.style.outlineOffset).toBe('4px'))
    expect(rowEl.style.borderRadius).toBe('8px')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('does not ring the row when the tab is the legacy agent-backend forward', async () => {
    // That tab forwards its `?highlight=` on to Settings > Agent Harness, so this
    // page must leave the anchor alone rather than consuming it here. (The redirect
    // navigates away, but the row — were it mounted — must never be ringed on this
    // route.)
    const { queryByTestId } = render(
      <MemoryRouter initialEntries={['/developer?tab=agent-backend&highlight=key:default-crewmate']}>
        <DeveloperPage />
      </MemoryRouter>,
    )
    // Give the 100ms highlight timer time to have fired had the hook been enabled.
    await new Promise(r => setTimeout(r, 200))
    const rowEl = queryByTestId('default-crewmate-row')
    // Either the redirect unmounted the config tab (row absent) or, if present, it
    // must carry no ring.
    if (rowEl) expect(rowEl.style.outlineOffset).toBe('')
  })
})
