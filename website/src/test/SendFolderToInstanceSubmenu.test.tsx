/**
 * The folder menu's "Send a copy to ▸ <instance>".
 *
 * Same split as SendToInstanceSubmenu.test.tsx, for the same jsdom reason (no
 * PointerEvent, so no live Radix submenu): the wrapper's self-hiding contract,
 * the row list against a plain Item stub, and the send loop on its own.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InstanceView } from '../api/client'
import { ApiError } from '../api/apiError'

const mocks = vi.hoisted(() => ({
  listInstances: vi.fn(),
  sendSessionToInstance: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import SendFolderToInstanceSubmenu, {
  FolderSendItems,
  folderSendStates,
  keysToSend,
  resetFolderSends,
  sendFolderSessions,
  startFolderSend,
  type FolderSendState,
} from '../components/SendFolderToInstanceSubmenu'
import { DropdownMenu, DropdownMenuContent } from '../components/ui/dropdown-menu'

function inst(over: Partial<InstanceView> & { id: string }): InstanceView {
  return {
    name: over.id,
    connection_method: 'ssm',
    status: { instance_id: over.id, state: 'connected' },
    ...over,
  } as InstanceView
}

function StubItem({ title, disabled, onSelect, children, ...rest }: {
  title?: string
  disabled?: boolean
  'aria-describedby'?: string
  onSelect?: (event: Event) => void
  children?: React.ReactNode
}) {
  return (
    <button type="button" title={title} disabled={disabled} data-testid="row"
      aria-describedby={rest['aria-describedby']}
      onClick={() => onSelect?.(new Event('select'))}>
      {children}
    </button>
  )
}

function items(over: Partial<React.ComponentProps<typeof FolderSendItems>> = {}) {
  const onSend = vi.fn()
  render(
    <FolderSendItems
      folderId="f1"
      instances={[inst({ id: 'crew-1' })]}
      states={{}}
      onSend={onSend}
      sendable={3}
      skipped={0}
      Item={StubItem}
      {...over}
    />,
  )
  return onSend
}

describe('SendFolderToInstanceSubmenu', () => {
  beforeEach(() => {
    mocks.listInstances.mockReset()
    mocks.sendSessionToInstance.mockReset()
    resetFolderSends()
  })

  it('renders no menu entry when no instances are configured', () => {
    mocks.listInstances.mockResolvedValue({ active: true, instances: [], warm_set_cap: 5 })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <DropdownMenu open>
          <DropdownMenuContent forceMount>
            <SendFolderToInstanceSubmenu folderId="f1" slotKeys={['a']} skipped={0} variant="dropdown" />
          </DropdownMenuContent>
        </DropdownMenu>
      </QueryClientProvider>,
    )
    expect(screen.queryByText('Send a copy to')).toBeNull()
  })

  function mountWithList() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <DropdownMenu open>
          <DropdownMenuContent forceMount>
            <SendFolderToInstanceSubmenu folderId="f1" slotKeys={['a']} skipped={0} variant="dropdown" />
          </DropdownMenuContent>
        </DropdownMenu>
      </QueryClientProvider>,
    )
  }

  it('keeps the entry when the crew list cannot be read, so the failure is not silent', async () => {
    mocks.listInstances.mockRejectedValue(new ApiError(500, 'gateway restarting', '{"error":"gateway restarting"}'))
    mountWithList()
    // jsdom cannot open a Radix submenu, so the visible proof is the trigger: an
    // empty-list fallback would have removed it.
    await waitFor(() => expect(screen.getByText('Send a copy to')).toBeTruthy())
  })

  it('stays hidden on the gateway\'s own instances_disabled 403, since the feature is simply off', async () => {
    mocks.listInstances.mockRejectedValue(new ApiError(
      403, 'instances feature is disabled',
      JSON.stringify({ error: 'instances feature is disabled', code: 'instances_disabled' }),
    ))
    mountWithList()
    await waitFor(() => expect(mocks.listInstances).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('Send a copy to')).toBeNull()
  })

  it('sends from a connected crew row and keeps a disconnected one disabled', () => {
    const onSend = items({
      instances: [inst({ id: 'crew-1' }), inst({ id: 'crew-2', status: { instance_id: 'crew-2', state: 'disconnected' } as InstanceView['status'] })],
    })
    const [up, down] = screen.getAllByTestId('row') as HTMLButtonElement[]
    expect(down.disabled).toBe(true)
    fireEvent.click(up)
    expect(onSend).toHaveBeenCalledWith('crew-1')
  })

  it('disables every crew row and says so when the folder has nothing to send', () => {
    items({ sendable: 0 })
    const rows = screen.getAllByTestId('row') as HTMLButtonElement[]
    expect(rows.every(r => r.disabled)).toBe(true)
    expect(rows).toHaveLength(2)
    expect(screen.getByText('No sessions to send')).toBeTruthy()
  })

  it('shows the private sessions it leaves out as visible text, not a hover hint', () => {
    items({ skipped: 2 })
    expect(screen.getByText('2 incognito or temporary sessions are not sent')).toBeTruthy()
    expect(screen.getAllByTestId('row')[0].title).toBe('crew-1')
  })

  it('reports progress, a full send and a partial one on the row', () => {
    const states: Record<string, FolderSendState> = {
      'crew-1': { kind: 'sending', done: 1, total: 3 },
    }
    const { rerender } = render(
      <FolderSendItems folderId="f1" instances={[inst({ id: 'crew-1' })]} states={states} onSend={vi.fn()}
        sendable={3} skipped={0} Item={StubItem} />,
    )
    expect(screen.getByText('1 of 3')).toBeTruthy()
    rerender(
      <FolderSendItems folderId="f1" instances={[inst({ id: 'crew-1' })]}
        states={{ 'crew-1': { kind: 'done', sent: 3, total: 3, transcriptOnly: 0, failed: [] } }}
        onSend={vi.fn()} sendable={3} skipped={0} Item={StubItem} />,
    )
    expect(screen.getByText('Sent: 3')).toBeTruthy()
    // A finished row stays clickable, and its hint says a click sends all again.
    expect((screen.getAllByTestId('row')[0] as HTMLButtonElement).title).toBe('crew-1 — Click to send another copy of all 3')
    rerender(
      <FolderSendItems folderId="f1" instances={[inst({ id: 'crew-1' })]}
        states={{ 'crew-1': { kind: 'done', sent: 3, total: 3, transcriptOnly: 1, failed: [] } }}
        onSend={vi.fn()} sendable={3} skipped={0} Item={StubItem} />,
    )
    // Both numbers: "transcript only" must not read as "only 1 was sent".
    expect(screen.getByText('Sent: 3 (1 transcript only)')).toBeTruthy()
    rerender(
      <FolderSendItems folderId="f1" instances={[inst({ id: 'crew-1' })]}
        states={{ 'crew-1': { kind: 'done', sent: 2, total: 3, transcriptOnly: 0, failed: ['c'], firstError: 'peer full' } }}
        onSend={vi.fn()} sendable={3} skipped={0} Item={StubItem} />,
    )
    // The failure is READABLE through the shared ErrorNotice, not only in a title=.
    expect(screen.getByText('2 of 3 sent')).toBeTruthy()
    expect(screen.getByText('peer full')).toBeTruthy()
    const alert = document.getElementById('folder-send-error-f1-crew-1')
    expect(alert).toBeTruthy()
    const rows = screen.getAllByTestId('row') as HTMLButtonElement[]
    // The crew row says a click retries only the failed one, and the agent
    // hand-off is a sibling menu item pointing back at the same notice.
    expect(rows[0].title).toContain('resend the 1 that failed')
    // ...and says it on screen too, not only on hover.
    expect(screen.getByText('Click to resend the 1 that failed')).toBeTruthy()
    expect(rows[0].getAttribute('aria-describedby')).toBe('folder-send-error-f1-crew-1')
    expect(rows[1].getAttribute('aria-describedby')).toBe('folder-send-error-f1-crew-1')
  })
})

describe('sendFolderSessions', () => {
  it('sends every session in order with the folder, and one refusal does not stop the rest', async () => {
    const send = vi.fn()
      .mockResolvedValueOnce({ resume_mode: 'session_load' })
      .mockRejectedValueOnce(new Error('peer refused'))
      .mockResolvedValueOnce({ resume_mode: 'prefix' })
    const seen: FolderSendState[] = []
    const out = await sendFolderSessions('crew-1', ['a', 'b', 'c'], send, s => seen.push(s))

    expect(send.mock.calls).toEqual([
      ['crew-1', 'a', true], ['crew-1', 'b', true], ['crew-1', 'c', true],
    ])
    expect(out).toEqual({ kind: 'done', sent: 2, total: 3, transcriptOnly: 1, failed: ['b'], firstError: 'peer refused' })
    expect(seen.filter(s => s.kind === 'sending').map(s => (s as { done: number }).done)).toEqual([0, 1, 2])
  })
})

describe('keysToSend', () => {
  it('retries only the failed sessions after a partial send, and all of them otherwise', () => {
    const keys = ['a', 'b', 'c']
    expect(keysToSend({ kind: 'idle' }, keys)).toEqual(keys)
    expect(keysToSend({ kind: 'done', sent: 3, total: 3, transcriptOnly: 0, failed: [] }, keys)).toEqual(keys)
    expect(keysToSend({ kind: 'done', sent: 1, total: 3, transcriptOnly: 0, failed: ['c', 'a'] }, keys)).toEqual(['a', 'c'])
  })
})

describe('folder send state outlives the menu', () => {
  beforeEach(() => resetFolderSends())

  it('records progress and the outcome with no component mounted at all', async () => {
    let release: (v: unknown) => void = () => {}
    const send = vi.fn(() => new Promise<{ resume_mode?: string }>(r => { release = r as (v: unknown) => void }))
    const done = startFolderSend('f1', 'crew-1', ['a', 'b'], send)

    // Nothing is rendered, yet the store says a send is running: a menu opened
    // now shows it (and its row refuses a second, overlapping send).
    expect(folderSendStates('f1', ['crew-1'])['crew-1']).toEqual({ kind: 'sending', done: 0, total: 2 })
    // Another folder sending to the same crew is a separate entry.
    expect(folderSendStates('f2', ['crew-1'])['crew-1']).toEqual({ kind: 'idle' })

    release({})
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2))
    release({})
    await done
    expect(folderSendStates('f1', ['crew-1'])['crew-1']).toMatchObject({ kind: 'done', sent: 2, failed: [] })
  })
})
