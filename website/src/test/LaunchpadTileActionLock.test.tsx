/**
 * A Library tile's management verbs all change the same app, so while ANY
 * action on that app runs, every verb in its ⋯ menu is locked — not just the
 * one that is running. An action on a DIFFERENT app locks nothing here.
 *
 * Real Radix menu (LibraryPageLaunchpad mocks it), because `disabled` on a
 * Radix item is what keeps `onSelect` from firing.
 */
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import LaunchpadTile from '../pages/apps/LaunchpadTile'
import type { LibraryApp } from '../pages/apps/useAppsData'

const ENABLED = {
  name: 'secretary', displayName: 'Secretary', version: '1.0.0',
  enabled: true, lifecycle: 'gateway', updateAvailable: true, manifest: {},
} as unknown as LibraryApp

const SYNCABLE = { ...ENABLED, updateAvailable: false } as LibraryApp

const DISABLED = { ...ENABLED, enabled: false, updateAvailable: false } as LibraryApp

function openMenu(app: LibraryApp, actionLoading: string | null) {
  const onAction = vi.fn()
  render(
    <LaunchpadTile
      app={app}
      pinned
      actionLoading={actionLoading}
      onTogglePin={() => {}}
      onAction={onAction}
      onOpen={() => {}}
      onDetail={() => {}}
    />,
  )
  const trigger = screen.getByRole('button', { name: 'More actions for Secretary' })
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter' })
  act(() => { vi.runAllTimers() })
  return onAction
}

function item(name: string) {
  return screen.getByRole('menuitem', { name })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it.each([
  ['secretary:disable'],
  ['secretary:uninstall'],
  ['secretary:update'],
])('locks Update, Disable and Uninstall while %s runs', (running) => {
  vi.useFakeTimers()
  const onAction = openMenu(ENABLED, running)
  for (const verb of ['Update', 'Disable', 'Uninstall']) {
    expect(item(verb).getAttribute('aria-disabled')).toBe('true')
  }
  fireEvent.click(item('Uninstall'))
  expect(onAction).not.toHaveBeenCalled()
})

it('locks Sync while another action on the app runs', () => {
  vi.useFakeTimers()
  openMenu(SYNCABLE, 'secretary:disable')
  expect(item('Sync').getAttribute('aria-disabled')).toBe('true')
})

it('locks Enable on a disabled app while its uninstall runs', () => {
  vi.useFakeTimers()
  openMenu(DISABLED, 'secretary:uninstall')
  expect(item('Enable').getAttribute('aria-disabled')).toBe('true')
  expect(item('Uninstall').getAttribute('aria-disabled')).toBe('true')
})

it('keeps Details open while an action runs', () => {
  vi.useFakeTimers()
  openMenu(ENABLED, 'secretary:disable')
  expect(item('Details').getAttribute('aria-disabled')).toBeNull()
})

it('leaves every verb free while only another app has an action running', () => {
  vi.useFakeTimers()
  const onAction = openMenu(ENABLED, 'notes:disable')
  for (const verb of ['Update', 'Disable', 'Uninstall']) {
    expect(item(verb).getAttribute('aria-disabled')).toBeNull()
  }
  fireEvent.click(item('Disable'))
  expect(onAction).toHaveBeenCalledWith('secretary', 'disable')
})
