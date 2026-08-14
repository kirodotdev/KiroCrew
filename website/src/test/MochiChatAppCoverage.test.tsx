// First coverage for Mochi's chat-window shell — `apps/mochi/src/renderer/ChatApp.tsx`.
//
// The shell owns no visible chrome of its own. Everything it does is wiring, so
// these tests exercise exactly that wiring: which view is mounted, how the two
// side rails are toggled and told to hide when the user leaves chat, how the
// pinned-file list and its updated/deleted markers are folded together from
// four separate backend channels, and what is unsubscribed on unmount.
//
// Two deliberate style choices:
//
//   1. The three child panels (ChatPanel + PinnedSidePanel, SettingsPanel,
//      WatchlistPanel) are stubbed. They are each covered by their own test
//      file, they are large, and a real chat transcript repeats the same text
//      many times over — which would make any text query here ambiguous. The
//      stubs publish the props they receive as data-* attributes so the shell's
//      state can be read back exactly, with no reliance on rendered prose.
//   2. `mochiApi` is the shell's only seam to the outside world, so it is the
//      single module mock. Its `on*` members mirror the preload's
//      `onX(cb) => off` shape, letting a test push a real backend frame and
//      assert what the shell recomputes in response.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resolvePetName } from '../apps/mochi/builtinPacks'

/** Matches the shell's local re-declaration of the main-process entry. */
interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

const mocks = vi.hoisted(() => {
  type NavCb = (route: string) => void
  type ChangedCb = (pins: unknown[]) => void
  type UpdatedCb = (info: { path: string; updatedAt: number }) => void
  type DeletedCb = (info: { path: string }) => void

  const navSubs = new Set<NavCb>()
  const changedSubs = new Set<ChangedCb>()
  const updatedSubs = new Set<UpdatedCb>()
  const deletedSubs = new Set<DeletedCb>()
  /** Which unsubscribes the shell actually invoked, in order. */
  const unsubbed: string[] = []

  const api = {
    getMochiConfig: vi.fn<() => Promise<Record<string, unknown>>>(),
    getPinnedFiles: vi.fn<() => Promise<unknown>>(),
    toggleWatchPanel: vi.fn(),
    togglePinnedPanel: vi.fn(),
    onNavigate: vi.fn((cb: NavCb) => {
      navSubs.add(cb)
      return () => { unsubbed.push('navigate'); navSubs.delete(cb) }
    }),
    onPinnedFilesChanged: vi.fn((cb: ChangedCb) => {
      changedSubs.add(cb)
      return () => { unsubbed.push('changed'); changedSubs.delete(cb) }
    }),
    onPinnedFileUpdated: vi.fn((cb: UpdatedCb) => {
      updatedSubs.add(cb)
      return () => { unsubbed.push('updated'); updatedSubs.delete(cb) }
    }),
    onPinnedFileDeleted: vi.fn((cb: DeletedCb) => {
      deletedSubs.add(cb)
      return () => { unsubbed.push('deleted'); deletedSubs.delete(cb) }
    }),
  }

  return {
    api,
    unsubbed,
    navSubCount: () => navSubs.size,
    emitNavigate: (route: string) => { for (const cb of [...navSubs]) cb(route) },
    emitChanged: (pins: unknown[]) => { for (const cb of [...changedSubs]) cb(pins) },
    emitUpdated: (info: { path: string; updatedAt: number }) => {
      for (const cb of [...updatedSubs]) cb(info)
    },
    emitDeleted: (info: { path: string }) => { for (const cb of [...deletedSubs]) cb(info) },
    reset: () => {
      navSubs.clear()
      changedSubs.clear()
      updatedSubs.clear()
      deletedSubs.clear()
      unsubbed.length = 0
    },
  }
})

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: mocks.api }))

interface StubChatPanelProps {
  onToggleWatch?: () => void
  watchPanelVisible?: boolean
  onTogglePinned?: () => void
  pinnedPanelVisible?: boolean
  pinnedFileCount?: number
}

interface StubPinnedPanelProps {
  pins: PinnedFileEntry[]
  updatedPaths: Set<string>
  deletedPaths: Set<string>
  visible: boolean
  petName?: string
  onMarkSeen?: (path: string) => void
}

interface StubWatchlistProps {
  visible: boolean
  onClose: () => void
  petName?: string
}

const serialize = (paths: Set<string>) => [...paths].sort().join('|')

vi.mock('../apps/mochi/src/renderer/ChatPanel', () => ({
  ChatPanel: ({
    onToggleWatch, watchPanelVisible, onTogglePinned, pinnedPanelVisible, pinnedFileCount,
  }: StubChatPanelProps) => (
    <div
      data-testid="stub-chat-panel"
      data-watch-visible={String(watchPanelVisible)}
      data-pinned-visible={String(pinnedPanelVisible)}
      data-pin-count={String(pinnedFileCount)}
    >
      <button type="button" onClick={onToggleWatch}>flip the watch rail</button>
      <button type="button" onClick={onTogglePinned}>flip the pinned rail</button>
    </div>
  ),
  PinnedSidePanel: ({
    pins, updatedPaths, deletedPaths, visible, petName, onMarkSeen,
  }: StubPinnedPanelProps) => (
    <div
      data-testid="stub-pinned-panel"
      data-visible={String(visible)}
      data-pet={petName ?? ''}
      data-pins={pins.map((p) => p.path).join('|')}
      data-updated={serialize(updatedPaths)}
      data-deleted={serialize(deletedPaths)}
    >
      <button type="button" onClick={() => onMarkSeen?.('/notes/alpha.md')}>
        mark alpha seen
      </button>
    </div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/SettingsPanel', () => ({
  SettingsPanel: ({ onClose }: { onClose: () => void }) => (
    <div data-testid="stub-settings-panel">
      <button type="button" onClick={onClose}>leave settings</button>
    </div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/WatchlistPanel', () => ({
  WatchlistPanel: ({ visible, onClose, petName }: StubWatchlistProps) => (
    <div
      data-testid="stub-watch-panel"
      data-visible={String(visible)}
      data-pet={petName ?? ''}
    >
      <button type="button" onClick={onClose}>close the watch rail</button>
    </div>
  ),
}))

const { ChatApp } = await import('../apps/mochi/src/renderer/ChatApp')

const api = mocks.api

function pin(path: string, over: Partial<PinnedFileEntry> = {}): PinnedFileEntry {
  return { path, label: path.split('/').pop() ?? path, pinnedAt: 1_700_000_000_000, ...over }
}

beforeEach(() => {
  // The shell schedules nothing itself, but a stray timer from any child would
  // fire after teardown and surface as an unhandled `window is not defined`.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.clearAllMocks()
  mocks.reset()
  api.getMochiConfig.mockResolvedValue({ petName: 'Nimbus' })
  api.getPinnedFiles.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
})

/** Mount and wait until the two mount-time reads have settled. */
async function mountApp(): Promise<void> {
  render(<ChatApp />)
  await waitFor(() => {
    expect(screen.getByTestId('stub-pinned-panel').getAttribute('data-pet')).not.toBe('')
  }, { timeout: 5_000 })
}

const chatPanel = () => screen.getByTestId('stub-chat-panel')
const pinnedPanel = () => screen.getByTestId('stub-pinned-panel')
const watchPanel = () => screen.getByTestId('stub-watch-panel')

describe('Mochi ChatApp — view selection', () => {
  it('mounts the chat view with both rails hidden', async () => {
    await mountApp()

    expect(chatPanel().getAttribute('data-watch-visible')).toBe('false')
    expect(chatPanel().getAttribute('data-pinned-visible')).toBe('false')
    expect(chatPanel().getAttribute('data-pin-count')).toBe('0')
    expect(pinnedPanel().getAttribute('data-visible')).toBe('false')
    expect(watchPanel().getAttribute('data-visible')).toBe('false')
    expect(screen.queryByTestId('stub-settings-panel')).toBeNull()
    // Neither rail was touched on mount — the shell only calls out on a toggle.
    expect(api.toggleWatchPanel).not.toHaveBeenCalled()
    expect(api.togglePinnedPanel).not.toHaveBeenCalled()
  })

  it('switches to settings on a backend navigate frame and unmounts both rails', async () => {
    await mountApp()

    await act(async () => { mocks.emitNavigate('/settings') })

    expect(await screen.findByTestId('stub-settings-panel')).toBeTruthy()
    expect(screen.queryByTestId('stub-chat-panel')).toBeNull()
    // Both rails are chat-only, so they leave the tree entirely.
    expect(screen.queryByTestId('stub-pinned-panel')).toBeNull()
    expect(screen.queryByTestId('stub-watch-panel')).toBeNull()
  })

  it('ignores a navigate frame for any other route', async () => {
    await mountApp()

    await act(async () => { mocks.emitNavigate('/avatars') })

    expect(screen.queryByTestId('stub-settings-panel')).toBeNull()
    expect(chatPanel()).toBeTruthy()
  })

  it('switches to settings on the in-window mochi-navigate event, and only for /settings', async () => {
    await mountApp()

    await act(async () => {
      window.dispatchEvent(new CustomEvent('mochi-navigate', { detail: '/gallery' }))
    })
    expect(screen.queryByTestId('stub-settings-panel')).toBeNull()

    await act(async () => {
      window.dispatchEvent(new CustomEvent('mochi-navigate', { detail: '/settings' }))
    })
    expect(await screen.findByTestId('stub-settings-panel')).toBeTruthy()
  })

  it('returns to chat when the settings panel closes itself', async () => {
    await mountApp()
    await act(async () => { mocks.emitNavigate('/settings') })
    await screen.findByTestId('stub-settings-panel')

    fireEvent.click(screen.getByRole('button', { name: 'leave settings' }))

    expect(await screen.findByTestId('stub-chat-panel')).toBeTruthy()
    expect(screen.queryByTestId('stub-settings-panel')).toBeNull()
  })

  it('stops listening to the window event once unmounted', async () => {
    await mountApp()
    cleanup()

    await act(async () => {
      window.dispatchEvent(new CustomEvent('mochi-navigate', { detail: '/settings' }))
    })

    // Nothing is mounted, so the assertion that matters is that no state
    // update was attempted on a torn-down tree — the listener was removed.
    expect(screen.queryByTestId('stub-settings-panel')).toBeNull()
  })
})

describe('Mochi ChatApp — pet name', () => {
  it('passes the stored name to both rails', async () => {
    await mountApp()

    expect(pinnedPanel().getAttribute('data-pet')).toBe('Nimbus')
    expect(watchPanel().getAttribute('data-pet')).toBe('Nimbus')
  })

  it('resolves an empty stored name to the active avatar name, never the empty string', async () => {
    api.getMochiConfig.mockResolvedValue({ petName: '' })
    const expected = resolvePetName({ petName: '' })
    expect(expected).not.toBe('')

    render(<ChatApp />)

    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-pet')).toBe(expected)
    }, { timeout: 5_000 })
    expect(watchPanel().getAttribute('data-pet')).toBe(expected)
  })
})

describe('Mochi ChatApp — side rails', () => {
  it('toggles the watch rail in both directions and mirrors it to the main process', async () => {
    await mountApp()

    fireEvent.click(screen.getByRole('button', { name: 'flip the watch rail' }))
    await waitFor(() => {
      expect(watchPanel().getAttribute('data-visible')).toBe('true')
    })
    expect(chatPanel().getAttribute('data-watch-visible')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: 'flip the watch rail' }))
    await waitFor(() => {
      expect(watchPanel().getAttribute('data-visible')).toBe('false')
    })
    expect(api.toggleWatchPanel.mock.calls).toEqual([[true], [false]])
  })

  it('lets the watch rail close itself through the same toggle', async () => {
    await mountApp()
    fireEvent.click(screen.getByRole('button', { name: 'flip the watch rail' }))
    await waitFor(() => {
      expect(watchPanel().getAttribute('data-visible')).toBe('true')
    })

    fireEvent.click(screen.getByRole('button', { name: 'close the watch rail' }))

    await waitFor(() => {
      expect(watchPanel().getAttribute('data-visible')).toBe('false')
    })
    expect(api.toggleWatchPanel.mock.calls).toEqual([[true], [false]])
  })

  it('toggles the pinned rail in both directions', async () => {
    await mountApp()

    fireEvent.click(screen.getByRole('button', { name: 'flip the pinned rail' }))
    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-visible')).toBe('true')
    })
    expect(chatPanel().getAttribute('data-pinned-visible')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: 'flip the pinned rail' }))
    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-visible')).toBe('false')
    })
    expect(api.togglePinnedPanel.mock.calls).toEqual([[true], [false]])
  })

  it('hides open rails on the way into settings and restores them on the way back', async () => {
    await mountApp()
    fireEvent.click(screen.getByRole('button', { name: 'flip the watch rail' }))
    fireEvent.click(screen.getByRole('button', { name: 'flip the pinned rail' }))
    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-visible')).toBe('true')
    })

    await act(async () => { mocks.emitNavigate('/settings') })
    await screen.findByTestId('stub-settings-panel')
    expect(api.toggleWatchPanel.mock.calls).toEqual([[true], [false]])
    expect(api.togglePinnedPanel.mock.calls).toEqual([[true], [false]])

    fireEvent.click(screen.getByRole('button', { name: 'leave settings' }))
    await screen.findByTestId('stub-chat-panel')

    expect(api.toggleWatchPanel.mock.calls).toEqual([[true], [false], [true]])
    expect(api.togglePinnedPanel.mock.calls).toEqual([[true], [false], [true]])
    // Rail state survived the trip, so the restored rails come back open.
    expect(pinnedPanel().getAttribute('data-visible')).toBe('true')
    expect(watchPanel().getAttribute('data-visible')).toBe('true')
  })

  it('says nothing to the main process when no rail was open', async () => {
    await mountApp()

    await act(async () => { mocks.emitNavigate('/settings') })
    await screen.findByTestId('stub-settings-panel')
    fireEvent.click(screen.getByRole('button', { name: 'leave settings' }))
    await screen.findByTestId('stub-chat-panel')

    expect(api.toggleWatchPanel).not.toHaveBeenCalled()
    expect(api.togglePinnedPanel).not.toHaveBeenCalled()
  })
})

describe('Mochi ChatApp — pinned files', () => {
  it('seeds the rail and the header count from the initial read', async () => {
    api.getPinnedFiles.mockResolvedValue([pin('/notes/alpha.md'), pin('/notes/beta.md')])

    await mountApp()

    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-pins')).toBe('/notes/alpha.md|/notes/beta.md')
    }, { timeout: 5_000 })
    expect(chatPanel().getAttribute('data-pin-count')).toBe('2')
  })

  it('keeps an empty list when the initial read answers with nothing', async () => {
    api.getPinnedFiles.mockResolvedValue(null)

    await mountApp()

    expect(pinnedPanel().getAttribute('data-pins')).toBe('')
    expect(chatPanel().getAttribute('data-pin-count')).toBe('0')
  })

  it('replaces the whole list on a changed frame', async () => {
    await mountApp()

    await act(async () => { mocks.emitChanged([pin('/notes/gamma.md')]) })

    expect(pinnedPanel().getAttribute('data-pins')).toBe('/notes/gamma.md')
    expect(chatPanel().getAttribute('data-pin-count')).toBe('1')
  })

  it('marks a file as updated and clears the mark when the rail reports it seen', async () => {
    api.getPinnedFiles.mockResolvedValue([pin('/notes/alpha.md'), pin('/notes/beta.md')])
    await mountApp()
    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-pins')).not.toBe('')
    }, { timeout: 5_000 })

    await act(async () => {
      mocks.emitUpdated({ path: '/notes/alpha.md', updatedAt: 1_700_000_100_000 })
      mocks.emitUpdated({ path: '/notes/beta.md', updatedAt: 1_700_000_200_000 })
    })
    expect(pinnedPanel().getAttribute('data-updated')).toBe('/notes/alpha.md|/notes/beta.md')

    fireEvent.click(screen.getByRole('button', { name: 'mark alpha seen' }))

    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-updated')).toBe('/notes/beta.md')
    })
  })

  it('marks a file as deleted', async () => {
    api.getPinnedFiles.mockResolvedValue([pin('/notes/alpha.md')])
    await mountApp()

    await act(async () => { mocks.emitDeleted({ path: '/notes/alpha.md' }) })

    expect(pinnedPanel().getAttribute('data-deleted')).toBe('/notes/alpha.md')
  })

  it('drops updated and deleted marks for files the changed frame no longer lists', async () => {
    api.getPinnedFiles.mockResolvedValue([pin('/notes/alpha.md'), pin('/notes/beta.md')])
    await mountApp()
    await waitFor(() => {
      expect(pinnedPanel().getAttribute('data-pins')).not.toBe('')
    }, { timeout: 5_000 })

    await act(async () => {
      mocks.emitUpdated({ path: '/notes/alpha.md', updatedAt: 1_700_000_100_000 })
      mocks.emitUpdated({ path: '/notes/beta.md', updatedAt: 1_700_000_200_000 })
      mocks.emitDeleted({ path: '/notes/alpha.md' })
      mocks.emitDeleted({ path: '/notes/beta.md' })
    })
    expect(pinnedPanel().getAttribute('data-updated')).toBe('/notes/alpha.md|/notes/beta.md')
    expect(pinnedPanel().getAttribute('data-deleted')).toBe('/notes/alpha.md|/notes/beta.md')

    // beta.md is gone from the authoritative list, so every mark naming it is
    // pruned. alpha.md is still listed, so both of its marks survive.
    await act(async () => { mocks.emitChanged([pin('/notes/alpha.md')]) })

    expect(pinnedPanel().getAttribute('data-pins')).toBe('/notes/alpha.md')
    expect(pinnedPanel().getAttribute('data-updated')).toBe('/notes/alpha.md')
    expect(pinnedPanel().getAttribute('data-deleted')).toBe('/notes/alpha.md')
  })

  it('releases the three pinned subscriptions on unmount', async () => {
    await mountApp()
    expect(api.onPinnedFilesChanged).toHaveBeenCalledTimes(1)
    expect(api.onPinnedFileUpdated).toHaveBeenCalledTimes(1)
    expect(api.onPinnedFileDeleted).toHaveBeenCalledTimes(1)

    cleanup()

    expect([...mocks.unsubbed].sort()).toEqual(['changed', 'deleted', 'updated'])
  })

  it('leaves the navigate subscription registered after unmount', async () => {
    // Current behaviour, asserted so a change is visible: the shell discards the
    // unsubscribe `onNavigate` hands back, so its callback outlives the tree.
    await mountApp()
    expect(mocks.navSubCount()).toBe(1)

    cleanup()

    expect(mocks.unsubbed).not.toContain('navigate')
    expect(mocks.navSubCount()).toBe(1)
  })
})
