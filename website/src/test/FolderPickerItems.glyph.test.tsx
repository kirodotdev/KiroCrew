/** The "Move to folder" picker draws each destination with that folder's own
 *  mark (emoji icon, or the lucide glyph tinted by its palette color), the way
 *  the sidebar row beside it does. Before, every row showed the same accent
 *  folder, so a colored or emoji folder was unrecognisable in the menu.
 *  FolderPickerItems takes the hosting menu's Item primitive, so a plain div
 *  stands in and the Radix Sub (flaky under jsdom) stays out of the test. */
import type React from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import type { ChatFolder } from '../types'

function Item({ title, children }: { title?: string; children?: React.ReactNode }) {
  return <div role="menuitem" title={title}>{children}</div>
}

const folders: ChatFolder[] = [
  { id: 'a', name: 'Rocket', order: 0, icon: '🚀' },
  { id: 'b', name: 'Painted', order: 1, color: '#22c55e' },
  { id: 'c', name: 'Plain', order: 2 },
]

afterEach(cleanup)

/** The picker reads the gateway connection from the store. An offline row
 *  trades its title for the offline reason, so the store is marked connected. */
function mount() {
  const store = createTestStore()
  store.dispatch(sseConnected())
  render(
    <Provider store={store}>
      <FolderPickerItems folders={folders} onPick={() => {}} Item={Item} />
    </Provider>,
  )
}

describe('FolderPickerItems folder glyphs', () => {
  it('renders the emoji icon of a folder that has one', () => {
    mount()
    const row = screen.getByTitle('Rocket')
    expect(row.textContent).toContain('🚀')
    expect(row.querySelector('svg')).toBeNull()
  })

  it('tints the glyph of a colored folder and leaves a plain one untinted', () => {
    mount()
    const painted = screen.getByTitle('Painted').querySelector('svg')
    const plain = screen.getByTitle('Plain').querySelector('svg')
    expect(painted).not.toBeNull()
    expect(plain).not.toBeNull()
    // A palette color washes the body with that color; an uncolored folder
    // keeps the plain surface fill. The fill attribute is asserted rather than
    // the inline stroke color, which the DOM test environment drops because it
    // does not parse color-mix().
    expect(painted!.getAttribute('fill')).toContain('#22c55e')
    expect(plain!.getAttribute('fill')).toBe('var(--bg-elevated)')
  })
})
