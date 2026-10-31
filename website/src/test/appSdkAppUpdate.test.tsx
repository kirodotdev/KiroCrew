/**
 * Tests for `useAppInfo().update` — the update state the host publishes to an app.
 *
 * The contract worth locking in is the three-way one: the field is ABSENT when
 * the host has not consulted anything, present with `available: false` when it
 * has and nothing is newer, and present with `available: true` otherwise. An
 * app that cannot tell the first from the second would render "up to date" on
 * no evidence, so absence is asserted as absence rather than as a falsy read.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import React from 'react'
import { AppApiProvider, useAppInfo, useAppUpdate, type AppInfo, type AppUpdate, type AppUpdateResult } from '../app-sdk/index'

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

describe('useAppUpdate().requestUpdate', () => {
  function getRequest(requestUpdateFn?: () => Promise<AppUpdateResult>) {
    let captured: (() => Promise<AppUpdateResult>) | null = null
    function Probe() {
      captured = useAppUpdate().requestUpdate
      return null
    }
    act(() => {
      render(
        React.createElement(
          AppApiProvider,
          {
            appName: 'test-app',
            appVersion: '1.0.0',
            allowedApiPaths: [],
            navigateFn: () => {},
            requestUpdateFn,
          },
          React.createElement(Probe),
        ),
      )
    })
    if (!captured) throw new Error('provider did not publish requestUpdate')
    return captured as () => Promise<AppUpdateResult>
  }

  it('calls the bridge the host bound, with no argument the app could steer', async () => {
    const bridge = vi.fn(async (): Promise<AppUpdateResult> => ({ status: 'updated', version: '1.1.0' }))
    const result = await getRequest(bridge)()
    expect(result).toEqual({ status: 'updated', version: '1.1.0' })
    expect(bridge).toHaveBeenCalledWith()
  })

  it('reports unsupported on a host that cannot update apps', async () => {
    expect(await getRequest()()).toEqual({ status: 'failed', version: '1.0.0', error: 'unsupported' })
  })
})
