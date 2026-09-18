/**
 * The chat's "Sign in to Kiro" affordance navigates to `KIRO_SIGN_IN_PATH`. That
 * string is only useful if the Developer page actually opens the pane that
 * renders the card AND lands on the card, so this pins the route to the pane and
 * the highlight to the anchor rather than to literals: a renamed tab key, a card
 * moved to another tab, or a page that stops mounting `useSettingHighlight` fails
 * here instead of leaving the error row pointing at the top of a long pane.
 *
 * The Agent Backend tab is replaced by a sentinel that mounts its anchor LATE,
 * the way the real tab renders the card only after its config query — the case
 * a first-tick lookup gets wrong. The card's own placement on that tab is pinned
 * in `AgentBackendTab.test.tsx`; here only routing and landing are under test.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'

vi.mock('../pages/developer/AgentBackendTab', () => ({
  AgentBackendTab: () => {
    // Mounts the anchor one tick late, standing in for the config query the
    // real tab awaits before it renders the card.
    const [loaded, setLoaded] = useState(false)
    useEffect(() => {
      const t = setTimeout(() => setLoaded(true), 250)
      return () => clearTimeout(t)
    }, [])
    return (
      <div data-testid="agent-backend-tab">
        {loaded ? <div data-testid="anchor" data-setting-key="kiro-sign-in" /> : 'Loading configuration…'}
      </div>
    )
  },
}))

// The sibling tabs are heavy and irrelevant here.
vi.mock('../pages/overview/MemoryGraphTab', () => ({ default: () => <div /> }))
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div data-testid="logs-tab" /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => <div />,
  AgentCfgTab: () => <div />,
}))

import DeveloperPage from '../pages/DeveloperPage'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'

function LocationProbe() {
  const { search } = useLocation()
  return <div data-testid="search">{search}</div>
}

beforeEach(() => {
  // Not available in jsdom; the hook scrolls the anchor into view.
  Element.prototype.scrollIntoView = vi.fn()
})

describe('DeveloperPage — Kiro sign-in deep link', () => {
  it('opens the Agent Backend tab, then rings the card once it mounts and consumes the highlight', async () => {
    render(
      <MemoryRouter initialEntries={[KIRO_SIGN_IN_PATH]}>
        <DeveloperPage />
        <LocationProbe />
      </MemoryRouter>,
    )
    expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
    expect(screen.queryByTestId('logs-tab')).toBeNull()
    // The anchor is not there yet: the highlight must survive, not be stripped
    // on the first tick as an unknown id. (The colon may or may not be
    // percent-encoded depending on the router's serialization.)
    expect(screen.getByTestId('search').textContent).toMatch(/highlight=key(:|%3A)kiro-sign-in/)

    const anchor = await screen.findByTestId('anchor')
    await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1))
    expect(anchor.style.outlineOffset).toBe('4px')
    await waitFor(() => expect(screen.getByTestId('search').textContent).not.toContain('highlight='))
    // The tab selection stays in the URL; only the consumed highlight goes.
    expect(screen.getByTestId('search').textContent).toContain('tab=agent-backend')
  })
})
