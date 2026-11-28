/**
 * Alt+K reference: a terminal-focus command (Clear terminal) can share its
 * chord with a global one (Search Everywhere on Cmd+K). The modal must say
 * which one fires, so the Clear terminal row carries a "focused terminal"
 * qualifier and an ordinary panel toggle does not.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { act, screen, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { i18nT } from '../i18n/t'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'
import ShortcutsModal from './ShortcutsModal'

const QUALIFIER = () => i18nT('components.shortcutsModal.terminal_focus_only')

/** An ordinary row holds its label directly; a qualified row wraps label and
 *  qualifier in one block, so its row is one level further up. */
function rowOf(label: string, qualified = false): HTMLElement {
  const block = screen.getByText(label).parentElement!
  return qualified ? block.parentElement! : block
}

describe('ShortcutsModal: terminal-focus qualifier', () => {
  beforeEach(() => {
    localStorage.clear()
    setTerminalEnabledFlag(true)
  })
  afterEach(() => {
    act(() => { setTerminalEnabledFlag(false) })
  })

  it('qualifies the Clear terminal row and no ordinary row', () => {
    renderWithProviders(<ShortcutsModal onClose={() => {}} />)
    expect(within(rowOf('Clear terminal', true)).getByText(QUALIFIER())).toBeTruthy()
    const ordinary = rowOf('Toggle session panel')
    // Guard the scope: the ordinary row is a single row (it has its key caps), not the whole list.
    expect(ordinary).toHaveTextContent('B')
    expect(ordinary).not.toHaveTextContent('Clear terminal')
    expect(within(ordinary).queryByText(QUALIFIER())).toBeNull()
    expect(screen.getAllByText(QUALIFIER())).toHaveLength(1)
  })

  it('finds the Clear terminal row by searching for the qualifier text', () => {
    renderWithProviders(<ShortcutsModal onClose={() => {}} />)
    fireEvent.change(screen.getByRole('textbox', { name: i18nT('components.shortcutsModal.search') }), { target: { value: 'focused terminal' } })
    expect(screen.getByText('Clear terminal')).toBeTruthy()
    expect(screen.queryByText('Toggle session panel')).toBeNull()
  })
})
