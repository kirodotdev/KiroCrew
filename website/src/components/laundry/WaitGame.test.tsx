import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { WaitGame, WAIT_GAME_DELAY_MS } from './WaitGame'
import { initI18n } from '../../i18n'

const engine = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn() }))
const host = vi.hoisted(() => ({ local: true }))
vi.mock('../../lib/loopbackHost', () => ({ isLoopbackHost: () => host.local }))
vi.mock('./engine', () => ({
  startLaundry: (...args: unknown[]) => { engine.start(...args); return { restart: () => {}, stop: engine.stop } },
}))

beforeEach(() => {
  initI18n('en')
  engine.start.mockClear()
  engine.stop.mockClear()
  host.local = true
  vi.useFakeTimers()
})
afterEach(() => { vi.useRealTimers() })

const space = () => act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })) })

describe('WaitGame', () => {
  it('renders nothing and starts no game until the wait has lasted 30 s', () => {
    render(<WaitGame />)
    act(() => { vi.advanceTimersByTime(WAIT_GAME_DELAY_MS - 1) })
    expect(screen.queryByTestId('wait-game')).toBeNull()
    expect(engine.start).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByTestId('wait-game')).toBeTruthy()
    expect(engine.start).toHaveBeenCalledTimes(1)
  })

  it('starts at once on Space when no control has focus', () => {
    render(<WaitGame />)
    space()
    expect(screen.getByTestId('wait-game')).toBeTruthy()
    expect(engine.start).toHaveBeenCalledTimes(1)
  })

  it('leaves Space to a focused control', () => {
    render(<><button type="button">Retry</button><WaitGame /></>)
    screen.getByRole('button', { name: 'Retry' }).focus()
    space()
    expect(screen.queryByTestId('wait-game')).toBeNull()
  })

  it('offers nothing on a dashboard reached through another name, such as a tunnel', () => {
    host.local = false
    render(<WaitGame />)
    space()
    act(() => { vi.advanceTimersByTime(WAIT_GAME_DELAY_MS) })
    expect(screen.queryByTestId('wait-game')).toBeNull()
    expect(engine.start).not.toHaveBeenCalled()
  })

  it('stops the game when the wait surface goes away', () => {
    const { unmount } = render(<WaitGame />)
    space()
    unmount()
    expect(engine.stop).toHaveBeenCalledTimes(1)
  })

  it('labels the game and its controls from the catalog', () => {
    render(<WaitGame surface="card" />)
    space()
    const game = screen.getByRole('group', { name: 'Laundry folding game' })
    expect(game.getAttribute('style')).toContain('var(--card)')
    expect(screen.getByRole('button', { name: 'Start over' })).toBeTruthy()
    expect(screen.getByText('Fold something while you wait.')).toBeTruthy()
  })
})
