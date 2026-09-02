import { describe, it, expect, vi } from 'vitest'
import reducer, {
  sseStatus,
  setUpdateProgress,
} from '../store/dashboardSlice'
import type { StatusData } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

describe('updateProgress in dashboardSlice', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('initial updateProgress is null', () => {
    expect(initial.updateProgress).toBeNull()
  })

  it('setUpdateProgress sets progress', () => {
    const state = reducer(initial, setUpdateProgress({ step: 'building', detail: 'Rebuilding…' }))
    expect(state.updateProgress).toEqual({ step: 'building', detail: 'Rebuilding…' })
  })

  it('setUpdateProgress(null) clears progress', () => {
    let state = reducer(initial, setUpdateProgress({ step: 'pulling', detail: 'Pulling…' }))
    state = reducer(state, setUpdateProgress(null))
    expect(state.updateProgress).toBeNull()
  })

  it('sseStatus syncs update_progress from backend', () => {
    const status = {
      uptime: '1h',
      sessions: 0,
      messages: 0,
      cron_jobs: 0,
      subagents: 0,
      lessons: 0,
      update_progress: { step: 'syncing', detail: 'Syncing workspace…' },
    } as StatusData
    const state = reducer(initial, sseStatus(status))
    expect(state.updateProgress).toEqual({ step: 'syncing', detail: 'Syncing workspace…' })
  })

  it('sseStatus with null update_progress clears existing progress', () => {
    let state = reducer(initial, setUpdateProgress({ step: 'building', detail: 'Building…' }))
    const status = {
      uptime: '1h',
      sessions: 0,
      messages: 0,
      cron_jobs: 0,
      subagents: 0,
      lessons: 0,
      update_progress: null,
    } as StatusData
    state = reducer(state, sseStatus(status))
    expect(state.updateProgress).toBeNull()
  })

  it('progress steps transition correctly', () => {
    let state = reducer(initial, setUpdateProgress({ step: 'pulling', detail: 'Pulling…' }))
    expect(state.updateProgress?.step).toBe('pulling')

    state = reducer(state, setUpdateProgress({ step: 'syncing', detail: 'Syncing…' }))
    expect(state.updateProgress?.step).toBe('syncing')

    state = reducer(state, setUpdateProgress({ step: 'building', detail: 'Building…' }))
    expect(state.updateProgress?.step).toBe('building')

    state = reducer(state, setUpdateProgress({ step: 'installing', detail: 'Installing…' }))
    expect(state.updateProgress?.step).toBe('installing')

    state = reducer(state, setUpdateProgress({ step: 'restarting', detail: 'Restarting…' }))
    expect(state.updateProgress?.step).toBe('restarting')
  })

  it('failed step is tracked', () => {
    const state = reducer(initial, setUpdateProgress({ step: 'failed', detail: 'Build error' }))
    expect(state.updateProgress?.step).toBe('failed')
    expect(state.updateProgress?.detail).toBe('Build error')
  })
})
