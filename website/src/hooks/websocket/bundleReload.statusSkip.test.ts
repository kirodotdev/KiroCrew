import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseDisconnected, sseStatus } from '../../store/dashboardSlice'
import { useBundleReload } from './bundleReload'
import type { StatusData } from '../../types'

function frame(over: Partial<StatusData> & Record<string, unknown> = {}): StatusData {
  return {
    uptime: '1m', sessions: 3, messages: 9, cron_jobs: 2, subagents: 0, lessons: 4,
    version: '1.0.0', bundle_id: 'b1', disabled_approval_modes: ['yolo'],
    uptime_secs: 60, messages_received: 9, gateway_rss_mb: 210, stats: { turns: 1 }, stats_summary: 's', cron: { jobs: [] },
    ...over,
  } as unknown as StatusData
}

function setup() {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  const actions: string[] = []
  const dispatch = vi.fn((a: { type: string }) => { actions.push(a.type); return store.dispatch(a) })
  const getDashboard = () => store.getState().dashboard
  const { result } = renderHook(() => useBundleReload(dispatch as never, getDashboard))
  const statusDispatches = () => actions.filter(t => t === sseStatus.type).length
  return { store, bundle: result.current, statusDispatches }
}

describe('useBundleReload status frames', () => {
  it('skips a frame whose only changes are the per-push counters', () => {
    const { store, bundle, statusDispatches } = setup()
    bundle.onDashboardStatus(frame())
    const held = store.getState().dashboard.status
    for (let i = 1; i <= 12; i++) {
      bundle.onDashboardStatus(frame({
        uptime: `${i}m`, uptime_secs: 60 + 5 * i, messages_received: 9 + i, gateway_rss_mb: 210 + i,
        stats: { turns: i }, stats_summary: `s${i}`, cron: { jobs: [i] }, yolo_remaining_secs: 300 - i,
      }))
    }
    expect(statusDispatches()).toBe(1)
    expect(store.getState().dashboard.status).toBe(held)
    expect(store.getState().dashboard.connected).toBe(true)
  })

  it('stores a frame when a field a surface reads changes', () => {
    const { store, bundle, statusDispatches } = setup()
    bundle.onDashboardStatus(frame())
    bundle.onDashboardStatus(frame({ sessions: 4 }))
    bundle.onDashboardStatus(frame({ sessions: 4, update_progress: { step: 'downloading', detail: '' } }))
    expect(statusDispatches()).toBe(3)
    expect(store.getState().dashboard.status?.sessions).toBe(4)
    expect(store.getState().dashboard.updateProgress).toEqual({ step: 'downloading', detail: '' })
  })

  it('stores an unchanged frame after a disconnect, so the socket reads connected again', () => {
    const { store, bundle, statusDispatches } = setup()
    bundle.onDashboardStatus(frame())
    store.dispatch(sseDisconnected())
    bundle.onDashboardStatus(frame({ uptime_secs: 65 }))
    expect(statusDispatches()).toBe(2)
    expect(store.getState().dashboard.connected).toBe(true)
  })

  it('stores an unchanged frame after another writer replaced the status', () => {
    const { store, bundle, statusDispatches } = setup()
    bundle.onDashboardStatus(frame())
    store.dispatch(sseStatus(frame({ update_available: true })))
    bundle.onDashboardStatus(frame({ uptime_secs: 65 }))
    expect(statusDispatches()).toBe(2)
    expect(store.getState().dashboard.status?.update_available).toBeUndefined()
  })
})
