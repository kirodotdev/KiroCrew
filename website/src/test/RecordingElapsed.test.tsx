import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import RecordingElapsed from '../components/RecordingElapsed'
import { formatElapsedClock } from '../lib/elapsedClock'

describe('formatElapsedClock', () => {
  it.each([
    [0, '0:00'],
    [7, '0:07'],
    [59, '0:59'],
    [60, '1:00'],
    [725, '12:05'],
    [3599, '59:59'],
    [3600, '1:00:00'],
    [3723, '1:02:03'],
    [36000, '10:00:00'],
  ])('reads %i seconds as %s', (seconds, clock) => {
    expect(formatElapsedClock(seconds)).toBe(clock)
  })

  it('drops the fraction instead of rounding up to a second that has not passed', () => {
    expect(formatElapsedClock(7.9)).toBe('0:07')
  })

  it.each([Number.NaN, Number.POSITIVE_INFINITY, -1, -0.5])('reads %s as 0:00', (bad) => {
    expect(formatElapsedClock(bad)).toBe('0:00')
  })
})

describe('RecordingElapsed', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  const reading = () => screen.getByTestId('voice-recording-elapsed').textContent

  it('starts at 0:00 and counts up from the moment it mounts', () => {
    render(<RecordingElapsed />)
    expect(reading()).toBe('0:00')
    act(() => { vi.advanceTimersByTime(7_000) })
    expect(reading()).toBe('0:07')
    act(() => { vi.advanceTimersByTime(58_000) })
    expect(reading()).toBe('1:05')
  })

  it('keeps counting past an hour: it shows no limit of its own', () => {
    render(<RecordingElapsed />)
    act(() => { vi.advanceTimersByTime(3_723_000) })
    expect(reading()).toBe('1:02:03')
  })

  it('reads the clock, so a tick that arrives late still shows the true time', () => {
    // A throttled background tab fires the interval rarely. Counting ticks
    // would fall behind; reading the clock on each one cannot. Here 90 seconds
    // pass on the clock while the interval fires exactly once.
    vi.useRealTimers()
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    let clock = 5_000
    const now = vi.spyOn(performance, 'now').mockImplementation(() => clock)
    try {
      render(<RecordingElapsed />)
      clock += 90_000
      act(() => { vi.advanceTimersByTime(250) })
      expect(reading()).toBe('1:30')
    } finally {
      now.mockRestore()
    }
  })

  it('holds the reading while frozen: the microphone is closed, so no time is being recorded', () => {
    const { rerender } = render(<RecordingElapsed />)
    act(() => { vi.advanceTimersByTime(12_000) })
    expect(reading()).toBe('0:12')
    rerender(<RecordingElapsed frozen />)
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(reading()).toBe('0:12')
  })

  it('starts again from 0:00 for the next recording', () => {
    const first = render(<RecordingElapsed />)
    act(() => { vi.advanceTimersByTime(40_000) })
    expect(reading()).toBe('0:40')
    first.unmount()
    render(<RecordingElapsed />)
    expect(reading()).toBe('0:00')
  })

  it('is a timer, so the row\'s live region does not announce every second', () => {
    render(<RecordingElapsed />)
    expect(screen.getByRole('timer')).toBe(screen.getByTestId('voice-recording-elapsed'))
  })

  it('stops its interval when it unmounts', () => {
    const clear = vi.spyOn(window, 'clearInterval')
    const { unmount } = render(<RecordingElapsed />)
    unmount()
    expect(clear).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })
})
