/**
 * Uninstall from a Library tile's ⋯ menu opens the page's confirm dialog,
 * which takes focus once (`focusOnMount`). The closing Radix menu must not
 * hand focus back to the ⋯ trigger behind that dialog afterwards, or Escape
 * and Tab act on the page underneath.
 *
 * Real Radix menu here (LibraryPageLaunchpad mocks it), because the restore
 * is Radix's own close behaviour.
 */
import { useRef, useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import LaunchpadTile from '../pages/apps/LaunchpadTile'
import type { LibraryApp } from '../pages/apps/useAppsData'
import { focusOnMount } from '../utils/focusOnMount'
import AppStoreDialog from '../components/appstore/AppStoreDialog'

const APP = {
  name: 'secretary', displayName: 'Secretary', version: '1.0.0',
  enabled: true, lifecycle: 'gateway', manifest: {},
} as unknown as LibraryApp

function Harness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <LaunchpadTile
        app={APP}
        pinned
        actionLoading={null}
        onTogglePin={() => {}}
        onAction={(_name, action) => { if (action === 'uninstall') setOpen(true) }}
        onOpen={() => {}}
        onDetail={() => {}}
      />
      {open && <div role="dialog" aria-label="Confirm uninstall" tabIndex={-1} ref={focusOnMount} />}
    </>
  )
}

afterEach(() => { vi.useRealTimers() })

it('keeps focus on the uninstall dialog after the menu closes', () => {
  // Fake timers, so the deferred open and Radix's deferred trigger restore
  // both run to completion before the assertion instead of after a guess.
  vi.useFakeTimers()
  render(<Harness />)
  const trigger = screen.getByRole('button', { name: 'More actions for Secretary' })
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter' })
  act(() => { vi.runAllTimers() })
  fireEvent.click(screen.getByRole('menuitem', { name: 'Uninstall' }))
  act(() => { vi.runAllTimers() })
  const dialog = screen.getByRole('dialog', { name: 'Confirm uninstall' })
  expect(vi.getTimerCount()).toBe(0)
  expect(document.activeElement).toBe(dialog)
})

/** The page's real shape: the tile hands the ⋯ trigger over with the verb, and
 *  the confirm (AppStoreDialog) returns focus to it on close. */
function PageHarness() {
  const [open, setOpen] = useState(false)
  const openerRef = useRef<HTMLElement | null>(null)
  return (
    <>
      <LaunchpadTile
        app={APP}
        pinned
        actionLoading={null}
        onTogglePin={() => {}}
        onAction={(_name, action, opener) => {
          if (action !== 'uninstall') return
          openerRef.current = opener ?? null
          setOpen(true)
        }}
        onOpen={() => {}}
        onDetail={() => {}}
      />
      {open && (
        <AppStoreDialog onClose={() => setOpen(false)} returnFocusTo={openerRef} aria-label="Confirm uninstall">
          <button type="button">Cancel</button>
        </AppStoreDialog>
      )}
    </>
  )
}

it('returns focus to the ⋯ trigger when the uninstall dialog closes', () => {
  vi.useFakeTimers()
  render(<PageHarness />)
  const trigger = screen.getByRole('button', { name: 'More actions for Secretary' })
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter' })
  act(() => { vi.runAllTimers() })
  fireEvent.click(screen.getByRole('menuitem', { name: 'Uninstall' }))
  act(() => { vi.runAllTimers() })
  const dialog = screen.getByRole('dialog', { name: 'Confirm uninstall' })
  expect(dialog.contains(document.activeElement)).toBe(true)

  fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
  act(() => { vi.runAllTimers() })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})
