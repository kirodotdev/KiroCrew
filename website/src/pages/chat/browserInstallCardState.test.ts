import { describe, expect, it } from 'vitest'
import type { BrowserInstallData, BrowserInstallJob } from '../../api/client'
import { browserInstallCardState } from './browserInstallCardState'

function data(overrides: Partial<BrowserInstallData> = {}): BrowserInstallData {
  return {
    installed: false,
    cli_path: null,
    cli_version: null,
    node_ok: true,
    node_version: '22.0.0',
    browser_ok: false,
    installing: false,
    last_error: null,
    token: false,
    install_job: null,
    ...overrides,
  }
}

function job(overrides: Partial<BrowserInstallJob> = {}): BrowserInstallJob {
  return {
    id: 'j1',
    kind: 'cli_setup',
    engine: null,
    status: 'running',
    stage: 'downloading_browser',
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:30Z',
    finished_at: null,
    elapsed_s: 30,
    error_code: null,
    error_detail: null,
    ...overrides,
  }
}

describe('browserInstallCardState', () => {
  it('surfaces a failed CLI setup even when filesystem detection passes', () => {
    const failed = job({ status: 'failed', error_detail: 'missing host libraries' })
    expect(browserInstallCardState(data({ installed: true, browser_ok: true, install_job: failed }), null, 0))
      .toEqual({ kind: 'failed', detail: 'missing host libraries' })
  })

  it('surfaces an interrupted CLI setup even when filesystem detection passes', () => {
    const interrupted = job({ status: 'interrupted', error_detail: 'setup interrupted' })
    expect(browserInstallCardState(data({ installed: true, browser_ok: true, install_job: interrupted }), null, 0))
      .toEqual({ kind: 'failed', detail: 'setup interrupted' })
  })

  it('reports installed after a successful CLI setup', () => {
    const succeeded = job({ status: 'succeeded' })
    expect(browserInstallCardState(data({ installed: true, browser_ok: true, install_job: succeeded }), null, 0))
      .toEqual({ kind: 'installed' })
  })

  it('reports a running job before installed or failed states', () => {
    expect(browserInstallCardState(data({ installed: true, browser_ok: true, install_job: job() }), null, 0))
      .toMatchObject({ kind: 'running', stage: 'downloading_browser' })
  })

  it('ignores a failed engine download when the CLI is installed', () => {
    const failed = job({
      kind: 'engine_download',
      engine: 'firefox',
      status: 'failed',
      error_detail: 'download failed',
    })
    expect(browserInstallCardState(data({ installed: true, browser_ok: true, install_job: failed }), null, 0))
      .toEqual({ kind: 'installed' })
  })
})
