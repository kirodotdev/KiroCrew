/**
 * A note a crewmate published shows that crewmate's face and label, from the
 * crewmates roster row with the note's slug and exact name; a note without
 * `member`, a crewmate no longer on the roster, and a roster still loading all
 * keep the kind icon. A failed roster read is reported once, with a Retry.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationBanner from '../components/notifications/NotificationBanner'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import NotificationCard from '../components/notifications/NotificationCard'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { noteMember } from '../components/notifications/CrewmateNoteFace'
import { api } from '../api/client'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    members: vi.fn(),
    ackNotification: vi.fn().mockResolvedValue({}),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))

const members = vi.mocked(api.members)

const base: Notification = { kind: 'agent', ts: '2026-10-07T10:00:00.000Z', title: 'PR ready', body: 'green', acked: false }
const fromAda: Notification = { ...base, member: { slug: 'ada', name: 'Ada' } }

const row = (over: Record<string, unknown>) => ({ slot_key: '', running: false, ...over })
function roster(rows: Array<Record<string, unknown>>) {
  members.mockResolvedValue({ members: rows.map(row) } as never)
}
const feedState = (notes: Notification[]) =>
  createTestStore({ notifications: { items: notes } as RootState['notifications'] })

beforeEach(() => { members.mockReset() })

describe('crewmate attribution on notifications', () => {
  it('draws the face and display label of the row with the same slug and exact name', async () => {
    roster([
      // Same slug, different exact name: slugs are lossy, so this is another crewmate.
      { name: 'ADA', slug: 'ada', display_name: 'Not Ada' },
      { name: 'Ada', slug: 'ada', display_name: 'Ada Lovelace', avatar: {} },
    ])
    renderWithProviders(<NotificationCard n={fromAda} onOpen={() => {}} openLabel="open" />)
    expect(await screen.findByTestId('notification-crewmate-face')).toBeInTheDocument()
    expect(screen.getByTestId('notification-crewmate-name')).toHaveTextContent('Ada Lovelace')
  })

  it('keeps the kind icon while the roster loads and when the crewmate is gone', async () => {
    let resolve: (v: unknown) => void = () => {}
    members.mockReturnValue(new Promise(r => { resolve = r }) as never)
    renderWithProviders(<NotificationCard n={fromAda} onOpen={() => {}} openLabel="open" />)
    expect(screen.queryByTestId('notification-crewmate-face')).toBeNull()
    await act(async () => { resolve({ members: [row({ name: 'Grace', slug: 'grace' })] }) })
    expect(screen.queryByTestId('notification-crewmate-face')).toBeNull()
    expect(screen.queryByTestId('notification-crewmate-name')).toBeNull()
  })

  it('never queries the roster for a note without a crewmate', async () => {
    renderWithProviders(<NotificationCard n={base} onOpen={() => {}} openLabel="open" />)
    await act(async () => {})
    expect(members).not.toHaveBeenCalled()
  })

  it('shows the crewmate in the detail panel', async () => {
    roster([{ name: 'Ada', slug: 'ada' }])
    renderWithProviders(<NotificationDetailPanel n={fromAda} onClose={() => {}} />)
    expect(await screen.findByTestId('notification-crewmate-face')).toBeInTheDocument()
    expect(screen.getByTestId('notification-crewmate-name')).toHaveTextContent('Ada')
  })

  it('reads a persisted member only in its exact shape', () => {
    expect(noteMember(fromAda)).toEqual({ slug: 'ada', name: 'Ada' })
    for (const bad of [undefined, 'ada', { slug: 'ada' }, { slug: '', name: 'Ada' }, { slug: 'ada', name: 3 }]) {
      expect(noteMember({ member: bad } as unknown as Notification)).toBeNull()
    }
  })
})

describe('a failed roster read', () => {
  const twoNotes = [fromAda, { ...fromAda, ts: '2026-10-07T10:01:00.000Z' }]

  it('is reported once in a feed showing crewmate notes, and the kind icon stays', async () => {
    members.mockRejectedValue(new Error('503'))
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store: feedState(twoNotes) })
    expect(await screen.findAllByTestId('notification-crewmate-roster-error')).toHaveLength(1)
    expect(screen.queryByTestId('notification-crewmate-face')).toBeNull()
  })

  it('is reported in the detail panel of a crewmate note', async () => {
    members.mockRejectedValue(new Error('503'))
    renderWithProviders(<NotificationDetailPanel n={fromAda} onClose={() => {}} />)
    expect(await screen.findByTestId('notification-crewmate-roster-error')).toBeInTheDocument()
  })

  it('is left to the detail panel while a crewmate note is selected', async () => {
    members.mockRejectedValue(new Error('503'))
    renderWithProviders(
      <>
        <NotificationFeed selectedTs={fromAda.ts} onSelect={() => {}} />
        <NotificationDetailPanel n={fromAda} onClose={() => {}} />
      </>,
      { store: feedState(twoNotes) },
    )
    expect(await screen.findAllByTestId('notification-crewmate-roster-error')).toHaveLength(1)
  })

  it('recovers through Retry', async () => {
    roster([{ name: 'Ada', slug: 'ada' }])
    members.mockRejectedValueOnce(new Error('503'))
    renderWithProviders(<NotificationDetailPanel n={fromAda} onClose={() => {}} />)
    fireEvent.click(await screen.findByTestId('notification-crewmate-roster-retry'))
    expect(await screen.findByTestId('notification-crewmate-face')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('notification-crewmate-roster-error')).toBeNull())
  })

  it('is not read at all by a feed with no crewmate notes', async () => {
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store: feedState([base]) })
    await act(async () => {})
    expect(members).not.toHaveBeenCalled()
    expect(screen.queryByTestId('notification-crewmate-roster-error')).toBeNull()
  })

  it('is reported once on the live banner, however many crewmate cards it shows', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    members.mockRejectedValue(new Error('503'))
    const bellRef = createRef<HTMLButtonElement>()
    renderWithProviders(
      <>
        <button ref={bellRef}>bell</button>
        <NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={() => {}} />
      </>,
      { route: '/settings' },
    )
    act(() => { dispatchLiveNotification(twoNotes[0]) })
    expect(await screen.findAllByTestId('notification-crewmate-roster-error')).toHaveLength(1)
    act(() => { dispatchLiveNotification(twoNotes[1]) })
    fireEvent.click(screen.getByTestId('notification-banner-count'))
    await waitFor(() => expect(screen.getAllByTestId('notification-banner-card')).toHaveLength(2))
    expect(await screen.findAllByTestId('notification-crewmate-roster-error')).toHaveLength(1)
  })
})
