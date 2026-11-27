/**
 * `CliPanel` (and xterm with it) loads on demand. While its chunk loads, the
 * pane shows a visible stand-in in the terminal's own frame, never a blank box,
 * and a hidden pane's stand-in stays hidden. Once the chunk is in, a new pane
 * renders the terminal in the same commit (no stand-in). The session helpers
 * callers use without xterm behave the same before and after the chunk arrives.
 */
import { beforeAll, beforeEach, describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/** Bound on each wait for an `import()` of the mocked `CliPanel` to settle:
 *  the 1000 ms default races a loaded coverage shard (docs/testing.md). */
const LAZY_CHUNK_TIMEOUT_MS = 5000

const h = vi.hoisted(() => ({ prewarm: vi.fn(), destroy: vi.fn(), disposeConnection: vi.fn() }))
vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  prewarmTerminal: h.prewarm,
}))
vi.mock('../utils/terminalRegistry', () => ({ disposeTerminalConnection: h.disposeConnection }))

import LazyCliPanel from '../components/LazyCliPanel'
import { __forgetTerminalModuleForTests, disposeTerminalSession, isSpeculativeTerminalChunkError, loadTerminalModule, preloadTerminal, prewarmTerminal, registerDestroyTerm } from '../components/terminalSessions'

describe('LazyCliPanel', () => {
  // Vitest hands a second `import()` of a mocked module that runs while
  // another is pending the real module, so resolve the mock once first and
  // keep one prewarm in flight at a time. Nothing has gone through
  // `loadTerminalModule` yet, so the first pane still takes the lazy path,
  // and each `prewarmTerminal` still waits on its own `import()`.
  beforeAll(async () => { await import('../components/CliPanel') })
  // Each test starts with the chunk not yet loaded, whatever ran before it.
  beforeEach(() => { __forgetTerminalModuleForTests(); h.prewarm.mockClear(); h.destroy.mockClear(); h.disposeConnection.mockClear() })

  it('shows a visible stand-in until the chunk loads, and a dispose in that window leaks nothing', async () => {
    renderWithProviders(<><LazyCliPanel sessionId="a" /><LazyCliPanel sessionId="b" visible={false} /></>)
    const standIns = screen.getAllByTestId('cli-panel-loading')
    expect(standIns).toHaveLength(2)
    expect(standIns[0]).toHaveTextContent('Starting terminal…')
    expect(standIns[0]).toHaveAttribute('role', 'status')
    expect(standIns[0]).toBeVisible()
    expect(standIns[1]).not.toBeVisible()

    // Closing a tab before the chunk loads closes its connection (there is no
    // xterm yet), and a prewarm asked for before that close builds nothing.
    prewarmTerminal('gone', 390, 300)
    disposeTerminalSession('gone')
    expect(h.disposeConnection).toHaveBeenCalledWith('gone')

    // Waits on the lazy boundary's `import('./CliPanel')`.
    expect(await screen.findByTestId('cli-a', undefined, { timeout: LAZY_CHUNK_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByTestId('cli-b')).toBeInTheDocument()
    expect(screen.queryByTestId('cli-panel-loading')).toBeNull()
    expect(h.prewarm).not.toHaveBeenCalled()

    // A prewarm nobody cancels does build the terminal. Waits on the
    // `import('./CliPanel')` inside `prewarmTerminal`.
    prewarmTerminal('kept', 390, 300)
    await vi.waitFor(() => expect(h.prewarm).toHaveBeenCalledWith('kept', 390, 300), { timeout: LAZY_CHUNK_TIMEOUT_MS })
    expect(h.prewarm).toHaveBeenCalledTimes(1)
  })

  it('renders the terminal in the same commit once the chunk has loaded', async () => {
    await loadTerminalModule()
    renderWithProviders(<LazyCliPanel sessionId="c" />)
    expect(screen.getByTestId('cli-c')).toBeInTheDocument()
    expect(screen.queryByTestId('cli-panel-loading')).toBeNull()
  })

  // main.tsx skips its stale-chunk reload for these, so a hover never reloads
  // the page over unsaved work; a pane waiting on the chunk keeps the reload.
  it('names a terminal chunk failure speculative only while no pane waits on the chunk', async () => {
    const chunkError = { message: 'Failed to fetch dynamically imported module: https://h/assets/CliPanel-AbC123.js' }
    // Waits on any earlier test's `import('./CliPanel')` to settle.
    await vi.waitFor(() => expect(isSpeculativeTerminalChunkError(chunkError)).toBe(false), { timeout: LAZY_CHUNK_TIMEOUT_MS })

    preloadTerminal()
    expect(isSpeculativeTerminalChunkError(chunkError)).toBe(true)
    expect(isSpeculativeTerminalChunkError({ message: 'Failed to fetch dynamically imported module: https://h/assets/SkillsPage-x.js' })).toBe(false)
    // WebKit's message names no URL.
    expect(isSpeculativeTerminalChunkError({ message: 'Importing a module script failed.' })).toBe(true)
    // Waits on the `import('./CliPanel')` inside `preloadTerminal` to settle.
    await vi.waitFor(() => expect(isSpeculativeTerminalChunkError(chunkError)).toBe(false), { timeout: LAZY_CHUNK_TIMEOUT_MS })

    const demanded = loadTerminalModule()
    expect(isSpeculativeTerminalChunkError(chunkError)).toBe(false)
    await demanded
  })

  it('disposes the cached xterm through the hook CliPanel registers', () => {
    registerDestroyTerm(h.destroy)
    disposeTerminalSession('a')
    expect(h.disposeConnection).toHaveBeenCalledWith('a')
    expect(h.destroy).toHaveBeenCalledWith('a')
  })
})
