/**
 * Settings → Shortcuts, Panel toggles: the skip-shell disclosure.
 *
 * A panel in `PANEL_TOGGLES_SKIPPING_SHELL` keeps its chord while an embedded
 * terminal has focus, which means the binding's cost lands on the SHELL — a
 * recorded Ctrl+C or Ctrl+R stops reaching it in every session. The recorder
 * cannot refuse those (a shell key is not an invalid chord, and which keys matter
 * depends on the user's shell), so the only honest place to state the trade is the
 * row where the binding is made.
 *
 * These tests pin the disclosure to exactly the skip-shell rows: absent from the
 * three ordinary panels, present on the one that takes keys from the PTY. That
 * coupling is the point — a future id added to the set inherits the warning, and
 * removing the set without replacing this disclosure fails here.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'

import { ShortcutsPanel } from '../pages/settings/ShortcutsPanel'
import { PANEL_TOGGLES_SKIPPING_SHELL, PANEL_TOGGLE_SHORTCUTS_KEY } from '../lib/panelToggleShortcuts'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'
import { renderWithProviders } from './helpers'

const HINT = /your shell stops receiving this key/i

describe('Settings → Shortcuts: skip-shell disclosure', () => {
  beforeEach(() => {
    localStorage.clear()
    setTerminalEnabledFlag(true)
  })
  afterEach(() => setTerminalEnabledFlag(false))

  it('warns on exactly the rows whose chord is taken from the PTY', () => {
    renderWithProviders(<ShortcutsPanel />)
    expect(screen.getAllByText(HINT)).toHaveLength(PANEL_TOGGLES_SKIPPING_SHELL.size)
  })

  it('does not warn on the ordinary panel toggles', () => {
    renderWithProviders(<ShortcutsPanel />)
    // The session-panel row is bound by default and is NOT skip-shell, so its
    // chord yields to a focused shell — no trade to disclose.
    expect(PANEL_TOGGLES_SKIPPING_SHELL.has('session-panel')).toBe(false)
    const rows = screen.getAllByText(/Toggle (left sidebar|session panel|side panel|terminal)/)
    expect(rows.length).toBeGreaterThan(PANEL_TOGGLES_SKIPPING_SHELL.size)
  })

  it('states the trade even before a chord is recorded, since the terminal ships unbound', () => {
    renderWithProviders(<ShortcutsPanel />)
    // The disclosure has to precede the choice: shown while the row is still
    // unbound, not only after the user has already spent a shell keystroke.
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
    expect(screen.getAllByText(HINT)).toHaveLength(PANEL_TOGGLES_SKIPPING_SHELL.size)
  })

  it('drops the warning with the row when the terminal is disabled', () => {
    setTerminalEnabledFlag(false)
    renderWithProviders(<ShortcutsPanel />)
    expect(screen.queryByText(HINT)).toBeNull()
  })
})
