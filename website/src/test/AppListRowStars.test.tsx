/**
 * GitHub star count on Discover list rows.
 *
 * The publisher bakes `stargazersCount` only into git-type third-party rows,
 * so FIELD PRESENCE is the display gate: a row with a sanitized number shows
 * the star badge, a row without the field (every built-in) shows nothing.
 * The count renders compact (`fmtCompact`) and locale-aware; the icon carries
 * an accessible name because icon+digits alone is not self-describing.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import AppListRow from '../components/appstore/AppListRow'
import type { RegistryApp } from '../components/appstore/types'
import { i18next } from '../i18n/all'

// Switchable per test: the star-count suite runs as a browser with no desktop
// requirement, the Enable-name suite as a browser that needs the desktop app.
const desktop = vi.hoisted(() => ({ needs: false }))
vi.mock('../lib/electron', () => ({ needsDesktopApp: () => desktop.needs }))

const noop = () => {}

function renderRow(app: Partial<RegistryApp>) {
  return render(
    <AppListRow
      app={{ name: 'demo-app', displayName: 'Demo App', description: '', version: '1.0.0', author: 'acme', tags: [], ...app } as RegistryApp}
      onOpen={noop}
      onGet={noop}
      onUpdate={noop}
      onEnable={noop}
    />,
  )
}

describe('AppListRow star count', () => {
  it('renders the compact count with an accessible star icon when the field is present', () => {
    renderRow({ stargazersCount: 15300 })
    // en compact form of 15300 is "15.3K".
    expect(screen.getByText('15.3K')).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub stars')).toBeInTheDocument()
  })

  it('renders a plain zero (a real count, distinct from the absent field)', () => {
    renderRow({ stargazersCount: 0 })
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub stars')).toBeInTheDocument()
  })

  it('renders no star badge when the field is absent (built-ins never carry it)', () => {
    renderRow({ origin: 'builtin', installed: true } as Partial<RegistryApp>)
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
  })

  it('renders no star badge for a non-numeric value that slipped past normalize', () => {
    renderRow({ stargazersCount: '999' } as unknown as Partial<RegistryApp>)
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
    expect(screen.queryByText('999')).toBeNull()
  })
})

// A desktop-only built-in's Enable button keeps its visible label as its
// accessible name (a voice-control user says the word printed on it) and
// carries the desktop hint as its description, instead of a name built by
// gluing two translated pieces with an English ". ".
describe('AppListRow desktop-only Enable name', () => {
  afterEach(async () => {
    desktop.needs = false
    await act(async () => { await i18next.changeLanguage('en') })
  })

  const builtinOff = { origin: 'builtin', installed: true, enabled: false } as Partial<RegistryApp>

  it('names the English button with its label and describes it with the hint', () => {
    desktop.needs = true
    renderRow(builtinOff)
    const btn = screen.getByRole('button', { name: 'Enable' })
    expect(btn).toHaveAccessibleDescription("Enabling works from here, but the app's own window needs the Kiro Crew desktop app.")
  })

  it('keeps the visible Japanese label as the name, with no English period', async () => {
    desktop.needs = true
    await act(async () => { await i18next.changeLanguage('ja') })
    renderRow(builtinOff)
    const btn = screen.getByRole('button', { name: '有効にする' })
    expect(btn).toHaveAccessibleDescription('ここから有効にできますが、アプリ独自のウィンドウには Kiro Crew デスクトップアプリが必要です。')
  })

  it('adds no description when the app does not need the desktop shell', () => {
    renderRow(builtinOff)
    expect(screen.getByRole('button', { name: 'Enable' })).not.toHaveAttribute('aria-describedby')
  })
})
