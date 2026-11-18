/**
 * The `menu` layout: a lazy part rendered inside a Radix dropdown menu.
 *
 * A failed chunk shows the passive notice, then Try Again, Reload page and the
 * agent hand-off as menu items (a menu's roving focus reaches items, never a
 * button nested in one). Try Again keeps the menu open, imports again and
 * renders the part in place, with focus left on the menu.
 */
import '../i18n/all'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import { lazyRetry } from '../components/lazyRetry'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { i18nT } from '../i18n/t'

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000
const TITLE = 'The update entry could not load.'

const chunk = { fail: true, imports: 0 }

// The part's chunk: an import that rejects the way a browser rejects a module
// script it could not fetch, until the test restores it.
function load() {
  chunk.imports += 1
  if (chunk.fail) return Promise.reject(new TypeError('Importing a module script failed.'))
  return Promise.resolve({ default: () => <DropdownMenuItem data-testid="part-loaded">Update ready</DropdownMenuItem> })
}

function renderInOpenMenu(askAgent?: boolean) {
  const Part = lazyRetry(load, { layout: 'menu', title: () => TITLE, scope: 'test-menu-part', askAgent })
  render(
    <DropdownMenu defaultOpen>
      <DropdownMenuTrigger>More</DropdownMenuTrigger>
      <DropdownMenuContent>
        <Part />
        <DropdownMenuItem>Pop out to window</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>,
  )
}

beforeEach(() => {
  chunk.fail = true
  chunk.imports = 0
  // React logs the caught render error; the boundary handling it is the point.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks() })

describe('lazyRetry menu layout', () => {
  it('shows the notice, Try Again, Reload page and the hand-off as menu items', async () => {
    renderInOpenMenu()
    const tryAgain = await screen.findByRole(
      'menuitem', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    const menu = screen.getByRole('menu')
    const notice = within(menu).getByRole('alert')
    expect(notice).toHaveTextContent(TITLE)
    expect(within(menu).getByRole('menuitem', { name: i18nT('components.errorBoundary.reload_page') })).toBeInTheDocument()
    const handoff = within(menu).getByRole('menuitem', { name: i18nT('components.askAgent.ask_the_agent') })
    expect(handoff, 'the hand-off is described by the notice').toHaveAttribute('aria-describedby', notice.id)
    expect(notice.id).not.toBe('')
    expect(within(menu).queryByRole('button'), 'no button nested in the menu').toBeNull()
    expect(tryAgain).toBeInTheDocument()
  })

  it('withholds the hand-off with askAgent: false', async () => {
    renderInOpenMenu(false)
    await screen.findByRole('menuitem', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS })
    expect(screen.getByRole('menuitem', { name: i18nT('components.errorBoundary.reload_page') })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: i18nT('components.askAgent.ask_the_agent') })).toBeNull()
  })

  it('Try Again keeps the menu open, imports again and renders the part in place', async () => {
    renderInOpenMenu()
    const tryAgain = await screen.findByRole(
      'menuitem', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(chunk.imports).toBe(1)

    chunk.fail = false
    tryAgain.focus()
    fireEvent.click(tryAgain)
    expect(await screen.findByTestId('part-loaded', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS })).toBeInTheDocument()
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
    const menu = screen.getByRole('menu')
    expect(menu, 'the menu stays open').toBeInTheDocument()
    expect(menu.contains(document.activeElement), 'focus stays in the menu').toBe(true)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
