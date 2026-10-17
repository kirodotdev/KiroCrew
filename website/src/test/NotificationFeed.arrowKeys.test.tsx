import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { groupShortcuts } from '../components/ShortcutsModal'
import { formatShortcut, shortcutLabel } from '../hooks/useKeyboardShortcuts'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const mkN = (over: Partial<Notification>): Notification => ({
  kind: 'cron', ts: '2026-07-24T10:00:00Z', title: 'Note', body: 'body', acked: false, ...over,
})

// The store holds oldest first and the feed renders newest first, so the rows
// read Newest, Middle, Oldest from the top.
const THREE = [mkN({ ts: '1', title: 'Oldest' }), mkN({ ts: '2', title: 'Middle' }), mkN({ ts: '3', title: 'Newest' })]

/** Mounts the feed under a host that owns the selection, as both real hosts
 *  do, and returns the ts of every row the feed asked it to select. */
function renderFeed(notifs: Notification[], variant: 'panel' | 'mac' = 'panel') {
  const selected: string[] = []
  function Host() {
    const [selectedTs, setSelectedTs] = useState<string | null>(null)
    return (
      <NotificationFeed
        variant={variant}
        selectedTs={selectedTs}
        onSelect={n => { selected.push(n.ts); setSelectedTs(n.ts) }}
      />
    )
  }
  const store = createTestStore({ notifications: { items: notifs } as RootState['notifications'] })
  renderWithProviders(<Host />, { store })
  return selected
}

const opener = (title: string) => screen.getByRole('button', { name: `Open notification: ${title}` })

/** Presses `key` where focus is; true when the feed claimed the keystroke. */
const press = (key: string, modifiers: KeyboardEventInit = {}) =>
  !fireEvent.keyDown(document.activeElement!, { key, ...modifiers })

describe('NotificationFeed: Up/Down step through the rows', () => {
  it.each(['panel', 'mac'] as const)('%s: selects the neighbouring row and carries focus to it', variant => {
    const selected = renderFeed(THREE, variant)
    opener('Newest').focus()

    expect(press('ArrowDown')).toBe(true)
    expect(selected).toEqual(['2'])
    expect(document.activeElement).toBe(opener('Middle'))

    expect(press('ArrowDown')).toBe(true)
    expect(press('ArrowUp')).toBe(true)
    expect(selected).toEqual(['2', '1', '2'])
    expect(document.activeElement).toBe(opener('Middle'))
  })

  it('leaves the key to the browser at either end and when a modifier is held', () => {
    const selected = renderFeed(THREE)
    opener('Oldest').focus()
    expect(press('ArrowDown')).toBe(false)
    opener('Newest').focus()
    expect(press('ArrowUp')).toBe(false)
    for (const modifier of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey']) {
      expect(press('ArrowDown', { [modifier]: true })).toBe(false)
    }
    expect(selected).toEqual([])
    expect(document.activeElement).toBe(opener('Newest'))
  })

  it('steps over a collapsed stack as one stop', () => {
    const selected = renderFeed([
      mkN({ ts: '1', title: 'Single' }),
      mkN({ ts: '2', title: 'Hidden in the stack', group_key: 'ci' }),
      mkN({ ts: '3', title: 'Stack head', group_key: 'ci' }),
    ])
    opener('Stack head').focus()
    expect(press('ArrowDown')).toBe(true)
    expect(selected).toEqual(['1'])
  })

  // The store holds oldest first, so the rows read Newest, then the stack
  // headed by its newest note, with "Under the head" collapsed beneath it.
  const STACKED = [
    mkN({ ts: '1', title: 'Under the head', group_key: 'ci' }),
    mkN({ ts: '2', title: 'Stack head', group_key: 'ci' }),
    mkN({ ts: '3', title: 'Newest' }),
  ]

  it('mac: landing on a collapsed stack expands it, as a click on it does', () => {
    const selected = renderFeed(STACKED, 'mac')
    opener('Newest').focus()

    expect(press('ArrowDown')).toBe(true)
    expect(selected).toEqual([])
    // Expanded, the head is labelled as an opener and the note under it has a row.
    expect(document.activeElement).toBe(opener('Stack head'))

    expect(press('ArrowDown')).toBe(true)
    expect(selected).toEqual(['1'])
    expect(document.activeElement).toBe(opener('Under the head'))
  })

  it('panel: landing on a collapsed stack opens its newest note, as a click on it does', () => {
    const selected = renderFeed(STACKED)
    opener('Newest').focus()
    expect(press('ArrowDown')).toBe(true)
    expect(selected).toEqual(['2'])
    expect(document.activeElement).toBe(opener('Stack head'))
  })

  it('leaves the key to an inner control of the row, so its focus and the selection stay put', () => {
    const selected = renderFeed([
      mkN({ ts: '1', title: 'Oldest' }),
      mkN({ ts: '2', title: 'Needs a decision', kind: 'approval', approval_id: 'ap-1' }),
    ])
    for (const inner of [
      screen.getByRole('button', { name: 'Approve' }),
      screen.getAllByRole('button', { name: 'Dismiss notification' })[0],
    ]) {
      inner.focus()
      expect(press('ArrowDown')).toBe(false)
      expect(document.activeElement).toBe(inner)
    }
    expect(selected).toEqual([])
  })
})

describe('NotificationFeed: Up/Down in the shortcuts reference', () => {
  it('lists both keys under Actions', () => {
    const actions = groupShortcuts('actions', true).map(def => [shortcutLabel(def), formatShortcut(def)])
    expect(actions).toEqual(expect.arrayContaining([['Previous notification', '↑'], ['Next notification', '↓']]))
  })
})
