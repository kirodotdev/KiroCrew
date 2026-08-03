/**
 * appStatusSlice — per-app runtime nav status (issue #520). Keyed by app name;
 * the sidebar reads it via selectAppNavState to render a corner status dot. The
 * slice is a dumb store: tone validation lives in the backend hook and the
 * render layer (unknown tone → neutral), so the reducer never rejects a frame.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import appStatusReducer, {
  setAppNavStatus,
  clearAppNavStatus,
  selectAppNavState,
} from './appStatusSlice'

function makeStore() {
  return configureStore({ reducer: { appStatus: appStatusReducer } })
}

describe('appStatusSlice', () => {
  it('returns null for an app with no reported status', () => {
    const store = makeStore()
    expect(selectAppNavState(store.getState(), 'midway-status')).toBeNull()
  })

  it('stores a reported status and returns it by app name', () => {
    const store = makeStore()
    store.dispatch(setAppNavStatus({ app: 'midway-status', tone: 'caution', label: 'Expiring 12m' }))
    expect(selectAppNavState(store.getState(), 'midway-status')).toEqual({ tone: 'caution', label: 'Expiring 12m' })
  })

  it('keeps per-app status independent', () => {
    const store = makeStore()
    store.dispatch(setAppNavStatus({ app: 'a', tone: 'positive', label: 'ok' }))
    store.dispatch(setAppNavStatus({ app: 'b', tone: 'critical', label: 'down' }))
    expect(selectAppNavState(store.getState(), 'a')).toEqual({ tone: 'positive', label: 'ok' })
    expect(selectAppNavState(store.getState(), 'b')).toEqual({ tone: 'critical', label: 'down' })
  })

  it('replaces the prior status for an app', () => {
    const store = makeStore()
    store.dispatch(setAppNavStatus({ app: 'a', tone: 'busy', label: 'running' }))
    store.dispatch(setAppNavStatus({ app: 'a', tone: 'positive', label: 'done' }))
    expect(selectAppNavState(store.getState(), 'a')).toEqual({ tone: 'positive', label: 'done' })
  })

  it('defaults a missing label to empty string', () => {
    const store = makeStore()
    store.dispatch(setAppNavStatus({ app: 'a', tone: 'busy' }))
    expect(selectAppNavState(store.getState(), 'a')).toEqual({ tone: 'busy', label: '' })
  })

  it('clears an app back to no-status', () => {
    const store = makeStore()
    store.dispatch(setAppNavStatus({ app: 'a', tone: 'critical', label: 'x' }))
    store.dispatch(clearAppNavStatus('a'))
    expect(selectAppNavState(store.getState(), 'a')).toBeNull()
  })
})
