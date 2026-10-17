/**
 * Pins the SettingsSubNav PATH-mode write path and the pop semantics of the
 * back control — the two areas of the basePath seam that the cold-deep-link
 * suites never execute (they mount and assert, but never click).
 *
 * Path mode (basePath set):
 *   - narrow drill-in PUSHES `${basePath}/<tab>/<key>` carrying the
 *     SUBNAV_PUSH_STATE marker, so the platform back gesture pops one level
 *   - the back control POPS (navigate(-1)) when the marker is present and
 *     REPLACES to `${basePath}/<tab>` on a cold deep link (no marker)
 *   - an invalid sub segment self-heals with a REPLACE to `${basePath}/<tab>`
 *   - select() is a no-op while the tab segment is absent (nothing may mint
 *     a `${basePath}//<key>` shape)
 *   - wide-mode canonicalization writes the implicit first item as a REPLACE
 *
 * Query mode (no basePath): the back control's POP branch — drill-in pushed
 * the entry with the marker, so back must navigate(-1), never replace-write.
 * This is asserted here because the pre-existing query suites only ever
 * enter panes via cold URLs (no marker → replace path), leaving a mutation
 * that scopes the push marker to path mode invisible to them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom'
import { SettingsSubNav } from '../components/SettingsSubNav'
import { SUBNAV_PUSH_STATE } from '../components/subNavParams'

let mockWidth: number | null = 400
vi.mock('../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, mockWidth],
}))
let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const ITEMS = [
  { key: 'slack', label: 'Slack' },
  { key: 'discord', label: 'Discord' },
] as const

function Probe() {
  const loc = useLocation()
  const navType = useNavigationType()
  const marker = (loc.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]
  return (
    <div data-testid="probe">
      {loc.pathname + loc.search}|{navType}|{marker ? 'marked' : 'unmarked'}
    </div>
  )
}

function renderSubNav(url: string, basePath?: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <SettingsSubNav items={ITEMS} listLabel="Chat channels" backLabel="Channels" basePath={basePath}>
        {active => <div data-testid="pane">{active ?? 'none'}</div>}
      </SettingsSubNav>
      <Probe />
    </MemoryRouter>,
  )
}

const probe = () => screen.getByTestId('probe').textContent

describe('SettingsSubNav path mode — write path and pop semantics', () => {
  beforeEach(() => {
    mockWidth = 400
    mobile = true
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
  })

  it('narrow drill-in pushes ${basePath}/<tab>/<key> with the SUBNAV_PUSH_STATE marker', () => {
    renderSubNav('/settings/channels', '/settings')
    fireEvent.click(screen.getByRole('button', { name: /Slack/ }))
    expect(probe()).toBe('/settings/channels/slack|PUSH|marked')
    expect(screen.getByTestId('pane').textContent).toBe('slack')
  })

  it('back POPS the entry the drill-in pushed — never a replace-write twin', () => {
    renderSubNav('/settings/channels', '/settings')
    fireEvent.click(screen.getByRole('button', { name: /Slack/ }))
    expect(probe()).toBe('/settings/channels/slack|PUSH|marked')
    fireEvent.click(screen.getByRole('button', { name: /Channels/ }))
    // POP, not REPLACE: a replace-write here would leave [list, list] twins
    // and the next platform back-swipe would visibly do nothing.
    expect(probe()).toBe('/settings/channels|POP|unmarked')
  })

  it('back on a cold deep link REPLACES to the list — popping would exit the app', () => {
    renderSubNav('/settings/channels/slack', '/settings')
    expect(screen.getByTestId('pane').textContent).toBe('slack')
    fireEvent.click(screen.getByRole('button', { name: /Channels/ }))
    expect(probe()).toBe('/settings/channels|REPLACE|unmarked')
  })

  it('self-heals an invalid sub segment with a REPLACE to the list', async () => {
    renderSubNav('/settings/channels/garbage', '/settings')
    await waitFor(() => expect(probe()).toBe('/settings/channels|REPLACE|unmarked'))
  })

  it('select() is a no-op without a tab segment — nothing mints ${basePath}//<key>', () => {
    renderSubNav('/settings', '/settings')
    fireEvent.click(screen.getByRole('button', { name: /Slack/ }))
    expect(probe()).toBe('/settings|POP|unmarked')
  })

  it('wide-mode canonicalization writes the implicit first item as a REPLACE', async () => {
    mockWidth = 900
    mobile = false
    renderSubNav('/settings/channels', '/settings')
    await waitFor(() => expect(probe()).toBe('/settings/channels/slack|REPLACE|unmarked'))
  })
})

describe('SettingsSubNav query mode — the pop branch is not path-mode-only', () => {
  beforeEach(() => {
    mockWidth = 400
    mobile = true
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
  })

  it('query-mode drill-in pushes ?sub= with the marker and back POPS it', () => {
    renderSubNav('/settings')
    fireEvent.click(screen.getByRole('button', { name: /Slack/ }))
    expect(probe()).toBe('/settings?sub=slack|PUSH|marked')
    fireEvent.click(screen.getByRole('button', { name: /Channels/ }))
    expect(probe()).toBe('/settings|POP|unmarked')
  })
})
