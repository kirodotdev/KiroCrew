/**
 * Tests for `useAppInfo().update` — the update state the host publishes to an app.
 *
 * The contract worth locking in is the three-way one: the field is ABSENT when
 * the host has not consulted anything, present with `available: false` when it
 * has and nothing is newer, and present with `available: true` otherwise. An
 * app that cannot tell the first from the second would render "up to date" on
 * no evidence, so absence is asserted as absence rather than as a falsy read.
 */
import { describe, it, expect } from 'vitest'
import { render, act } from '@testing-library/react'
import React from 'react'
import { AppApiProvider, useAppInfo, type AppInfo, type AppUpdate } from '../app-sdk/index'

function getInfo(appUpdate?: AppUpdate): AppInfo {
  let captured: AppInfo | null = null

  function Probe() {
    captured = useAppInfo()
    return null
  }

  act(() => {
    render(
      React.createElement(
        AppApiProvider,
        {
          appName: 'test-app',
          appVersion: '1.0.0',
          appUpdate,
          allowedApiPaths: [],
          allowedEvents: [],
          subscribeFn: () => () => {},
          navigateFn: () => {},
          notifyFn: () => {},
        },
        React.createElement(Probe),
      ),
    )
  })

  if (!captured) throw new Error('provider did not publish app info')
  return captured
}

describe('useAppInfo().update', () => {
  it('is absent when the host has no cached answer', () => {
    const info = getInfo()
    expect('update' in info).toBe(false)
    expect(info.update).toBeUndefined()
  })

  it('reports an available update', () => {
    const update = {
      available: true,
      latestVersion: '1.1.0',
      checkedAt: '2023-11-14T22:13:20+00:00',
    }
    expect(getInfo(update).update).toEqual(update)
  })

  it('distinguishes a checked "nothing newer" from an unchecked app', () => {
    const info = getInfo({
      available: false,
      latestVersion: '1.0.0',
      checkedAt: '2023-11-14T22:13:20+00:00',
    })
    expect('update' in info).toBe(true)
    expect(info.update?.available).toBe(false)
  })

  it('keeps a version whose cache timestamp could not be read', () => {
    const info = getInfo({ available: true, latestVersion: '2.0.0', checkedAt: null })
    expect(info.update?.latestVersion).toBe('2.0.0')
    expect(info.update?.checkedAt).toBeNull()
  })

  it('leaves the rest of the app info untouched', () => {
    const info = getInfo({ available: true, latestVersion: '1.1.0', checkedAt: null })
    expect(info.name).toBe('test-app')
    expect(info.version).toBe('1.0.0')
  })
})
