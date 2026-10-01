import { describe, it, expect } from 'vitest'

import { ApiError } from '../../api/apiError'
import { browserInstallConflictJob, type BrowserInstallData, type BrowserInstallJob } from '../../api/client'
import {
  currentActivity,
  elapsedSeconds,
  engineRowState,
  engineStatus,
  installBlock,
} from './browserInstallState'

function data(overrides: Partial<BrowserInstallData> = {}): BrowserInstallData {
  return {
    installed: true,
    cli_path: null,
    cli_version: '0.1.18',
    node_ok: true,
    node_version: '22.0.0',
    browser_ok: true,
    installing: false,
    last_error: null,
    token: false,
    browsers: { chromium: true, firefox: false, webkit: false },
    install_job: null,
    ...overrides,
  }
}

function job(overrides: Partial<BrowserInstallJob> = {}): BrowserInstallJob {
  return {
    id: 'j1',
    kind: 'engine_download',
    engine: 'firefox',
    status: 'running',
    stage: 'downloading_browser',
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:10Z',
    finished_at: null,
    elapsed_s: 10,
    error_code: null,
    error_detail: null,
    ...overrides,
  }
}

describe('browserInstallState', () => {
  it('lets the gateway job win over this tab\'s pending request', () => {
    const a = currentActivity(data({ installing: true, install_job: job() }), { kind: 'engine_download', engine: 'webkit' }, 0)
    expect(a).toEqual({ source: 'job', job: job() })
  })

  it('attributes a pending request only while no running job is reported', () => {
    const a = currentActivity(data(), { kind: 'engine_download', engine: 'webkit' }, 5)
    expect(a).toEqual({ source: 'pending', request: { kind: 'engine_download', engine: 'webkit' }, startedAt: 5 })
  })

  it('treats `installing` without a running job as unattributed, never idle', () => {
    expect(currentActivity(data({ installing: true, install_job: undefined }), null, 0)).toEqual({ source: 'legacy' })
    expect(currentActivity(data({ installing: true, install_job: job({ status: 'failed' }) }), null, 0)).toEqual({
      source: 'legacy',
    })
    expect(currentActivity(data(), null, 0)).toBeNull()
  })

  it('reads engine status from browser_status, then the booleans, then unknown', () => {
    expect(engineStatus(data({ browser_status: { firefox: 'unknown' } }), 'firefox')).toBe('unknown')
    expect(engineStatus(data(), 'chromium')).toBe('downloaded')
    expect(engineStatus(data(), 'firefox')).toBe('missing')
    expect(engineStatus(data({ browsers: undefined }), 'webkit')).toBe('unknown')
  })

  it('offers retry only on the engine whose last download failed', () => {
    const d = data({ install_job: job({ status: 'interrupted', error_code: 'interrupted' }) })
    expect(engineRowState(d, 'firefox', null)).toBe('retry')
    expect(engineRowState(d, 'webkit', null)).toBe('missing')
  })

  it('orders block reasons from least known to most specific', () => {
    const running = { source: 'job', job: job() } as const
    expect(installBlock(running, true, true)).toEqual({ reason: 'status_unavailable' })
    expect(installBlock(running, false, true)).toEqual({ reason: 'checking' })
    expect(installBlock(running, false, false)).toEqual({ reason: 'engine_busy', engine: 'firefox' })
    expect(installBlock({ source: 'job', job: job({ kind: 'cli_setup', engine: null }) }, false, false)).toEqual({
      reason: 'cli_busy',
    })
    expect(installBlock({ source: 'legacy' }, false, false)).toEqual({ reason: 'busy_generic' })
    expect(installBlock(null, false, false)).toBeNull()
  })

  it('advances a running job from the time its status arrived', () => {
    expect(elapsedSeconds({ source: 'job', job: job({ elapsed_s: 10 }) }, null, 1_000, 4_000)).toBe(13)
    expect(elapsedSeconds(null, job({ status: 'succeeded', elapsed_s: 42 }), 0, 99_000)).toBe(42)
    expect(elapsedSeconds({ source: 'legacy' }, null, 0, 1)).toBeNull()
  })
})

describe('browserInstallConflictJob', () => {
  it('reads the active job from a 409 install_already_running body', () => {
    const active = job({ engine: 'webkit' })
    const err = new ApiError(409, 'busy', JSON.stringify({ code: 'install_already_running', install_job: active }))
    expect(browserInstallConflictJob(err)).toEqual(active)
  })

  it('returns null for other statuses, bodies without a job, and non-JSON bodies', () => {
    expect(browserInstallConflictJob(new ApiError(500, 'x', JSON.stringify({ install_job: job() })))).toBeNull()
    expect(browserInstallConflictJob(new ApiError(409, 'x', '{"error":"busy"}'))).toBeNull()
    expect(browserInstallConflictJob(new ApiError(409, 'x', '<html>proxy</html>'))).toBeNull()
    expect(browserInstallConflictJob(new ApiError(409, 'x', JSON.stringify({ install_job: { id: 1 } })))).toBeNull()
    expect(browserInstallConflictJob(new Error('network'))).toBeNull()
  })
})
