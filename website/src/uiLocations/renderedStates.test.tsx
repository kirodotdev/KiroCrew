/**
 * Rendered state pairs for find_ui prerequisites: each pair renders the REAL
 * component in both states a descriptor names and checks that the marker is on
 * the actionable element in one and absent in the other. The structural
 * goldens (`*.locations.test.ts`) prove what the index SAYS; these prove the
 * render guard agrees, so a changed guard cannot leave the index quietly wrong.
 *
 * To add a pair: render the host twice (or flip its state), then call
 * `expectActionable(id, role)` for the state that draws it and
 * `expectAbsent(id)` for the one that does not.
 */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { ReactElement } from 'react'

import { renderWithProviders } from '../test/helpers'
import { UI_LOCATION_ATTR } from './uiLocation'
import type { UiLocationId } from './descriptors'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

/** Every api call resolves; the few shapes the hosts below read are named. */
const apiShapes = vi.hoisted(() => ({
  crons: { jobs: [] as unknown[] },
  cronScript: {
    source: 'def run(ctx): pass\n', file: 'job.py', function: 'run', truncated: false, reviewable: true, sha256: 'a'.repeat(64),
  },
} as Record<string, unknown>))
vi.mock('../api/client', () => {
  const fns: Record<string, unknown> = {}
  const arrays = new Set(['cronFolders', 'models', 'secretsList'])
  return {
    api: new Proxy({}, {
      get: (_t, k: string) => {
        fns[k] ??= vi.fn(async () => (k in apiShapes ? apiShapes[k] : arrays.has(k) ? [] : {}))
        return fns[k]
      },
    }),
  }
})

globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const marked = (id: UiLocationId) => document.querySelectorAll(`[${UI_LOCATION_ATTR}="${id}"]`)

/** The marker is on exactly one element, and that element is the control a person operates. */
function expectActionable(id: UiLocationId, role: 'button' | 'menuitem' | 'checkbox', name?: string | RegExp) {
  const els = marked(id)
  expect(els.length, `${id} marked once`).toBe(1)
  const el = els[0] as HTMLElement
  const byRole = screen.getAllByRole(role, name !== undefined ? { name } : {})
  expect(byRole, `${id} is the ${role} itself, not a wrapper or a child`).toContain(el)
}

function expectAbsent(id: UiLocationId) {
  expect(marked(id).length, `${id} not drawn in this state`).toBe(0)
}

// ---------------------------------------------------------------- composer
import ChatInput from '../components/ChatInput'

const COLLAPSED_KEY = 'mc-composer-collapsed'
const composer = (props: Record<string, unknown> = {}): ReactElement => (
  <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={vi.fn()} collapsible {...props} />
)

describe('composer: collapsed / expanded (composer_collapsed)', () => {
  beforeEach(() => localStorage.removeItem(COLLAPSED_KEY))

  it('collapsed: only the Expand bar is drawn, and it is the button', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    renderWithProviders(composer({ value: 'hi' }))
    expectActionable('composer.expand', 'button', 'Show the message input')
    for (const id of ['composer.send', 'composer.add-menu', 'composer.stop'] as const) expectAbsent(id)
  })

  it('expanded: the controls are drawn and the Expand bar is not', () => {
    renderWithProviders(composer({ value: 'hi' }))
    expectAbsent('composer.expand')
    expectActionable('composer.send', 'button', 'Send')
    expectActionable('composer.add-menu', 'button', 'Add files & options')
  })
})

describe('composer: Stop needs nothing waiting to send (message_box_empty)', () => {
  beforeEach(() => localStorage.removeItem(COLLAPSED_KEY))
  const running = { isRunning: true, onStop: vi.fn() }

  it('running with an empty box and no attachment: Stop is the button', () => {
    renderWithProviders(composer(running))
    expectActionable('composer.stop', 'button', 'Stop generation')
  })

  it('running with typed text: no Stop', () => {
    renderWithProviders(composer({ ...running, value: 'more' }))
    expectAbsent('composer.stop')
  })

  it('running with only an attached file: no Stop either', () => {
    renderWithProviders(composer({ ...running, pendingFiles: ['/tmp/a.png'] }))
    expectAbsent('composer.stop')
  })
})

// ---------------------------------------------------------------- notifications
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import type { Notification } from '../types'

describe('notification detail: read / unread (notification_read)', () => {
  const note: Notification = { kind: 'agent', ts: '2026-09-22T07:00:00Z', title: 'Hello', body: 'x', acked: true }

  it('read: Mark unread is the button', () => {
    renderWithProviders(<NotificationDetailPanel n={note} onClose={() => {}} />)
    expectActionable('notifications.detail.mark-unread', 'button', /Mark unread/)
  })

  it('unread: no Mark unread (Mark read stands there)', () => {
    renderWithProviders(<NotificationDetailPanel n={{ ...note, acked: false }} onClose={() => {}} />)
    expectAbsent('notifications.detail.mark-unread')
    expect(screen.getByRole('button', { name: /Mark read/ })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------- apps (Library card)
import LaunchpadTile from '../pages/apps/LaunchpadTile'

describe('Library card: the way to Details for an app that opens (app_tile_menu_open)', () => {
  it('clicking an openable card launches it; only the ⋯ menu reaches Details', async () => {
    const onOpen = vi.fn()
    const onDetail = vi.fn()
    // Enabled with a page: `openable`, the common case the old wording got wrong.
    const app = {
      name: 'demo', enabled: true, installed: true, lifecycle: 'gateway',
      manifest: { name: 'demo', ui: { pages: [{ route: '/apps/demo', title: 'Demo' }] } },
    } as unknown as Parameters<typeof LaunchpadTile>[0]['app']
    renderWithProviders(
      <LaunchpadTile app={app} pinned={false} pinnable={false} actionLoading={null}
        onTogglePin={vi.fn()} onAction={vi.fn()} onOpen={onOpen} onDetail={onDetail} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'demo' }))
    expect(onOpen).toHaveBeenCalled()
    expect(onDetail).not.toHaveBeenCalled()
    expectAbsent('apps.library.tile-details')
    fireEvent.keyDown(screen.getByRole('button', { name: /more actions/i }), { key: 'Enter' })
    await screen.findByRole('menuitem', { name: /Details/ })
    expectActionable('apps.library.tile-details', 'menuitem', /Details/)
    fireEvent.click(screen.getByRole('menuitem', { name: /Details/ }))
    expect(onDetail).toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------- schedule
import SchedulePage from '../pages/SchedulePage'

const job = {
  id: 'job-1', name: 'Nightly report', schedule: 'every 1d', message: 'm', enabled: true,
  script: 'job.py:run', secret_env_pending: { MY_TOKEN: 'slack-sandbox' },
}

describe('schedule: List / Calendar (schedule_list_view) and Details / Logs (job_details_tab)', () => {
  beforeEach(() => { apiShapes.crons = { jobs: [job] } })

  it('List view draws New folder and Select all; Calendar hides both', async () => {
    renderWithProviders(<SchedulePage />)
    await screen.findByRole('checkbox', { name: 'Select Nightly report' })
    expectActionable('schedule.new-folder', 'button', /New folder/)
    expectActionable('schedule.select-all', 'checkbox', 'Select all jobs')
    fireEvent.click(screen.getByRole('radio', { name: /Calendar/ }))
    await waitFor(() => expectAbsent('schedule.select-all'))
    expectAbsent('schedule.new-folder')
  })

  it("the secret approval is in the job's Details tab, not Logs", async () => {
    renderWithProviders(<SchedulePage />)
    const row = (await screen.findByRole('checkbox', { name: 'Select Nightly report' })).closest('tr')!
    fireEvent.click(within(row).getByText('Nightly report'))
    await waitFor(() => expect(marked('schedule.secret-approve').length).toBe(1))
    // Settled, not a first-paint flash: the panel's source and secrets reads land first.
    await waitFor(() => expect(screen.getByRole('button', { name: /Approve/ })).toBeEnabled())
    expectActionable('schedule.secret-approve', 'button', /Approve/)
    // The panel's Details / Logs switch may collapse to a menu at this width.
    const trigger = screen.queryByRole('radio', { name: 'Logs' })
      ?? (fireEvent.click(screen.getByRole('button', { name: /^Details/, expanded: false })), screen.getByRole('radio', { name: 'Logs' }))
    await act(async () => { fireEvent.click(trigger) })
    // The Logs tab really is showing (its body replaced Details)…
    await screen.findByText(/No execution history yet|Loading logs/i)
    // …and the approval went with Details.
    expectAbsent('schedule.secret-approve')
  })
})
