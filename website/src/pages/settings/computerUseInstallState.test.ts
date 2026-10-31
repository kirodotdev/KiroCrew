import { describe, expect, it } from 'vitest'

import type { AgentDesktopStatus } from '../../api/client'
import {
  AGENT_DESKTOP_POLL_MS,
  failedJob,
  installAction,
  jobRunning,
  permissionsIncomplete,
  permissionVariant,
  statusPollInterval,
  versionState,
} from './computerUseInstallState'

function status(overrides: Partial<AgentDesktopStatus> = {}): AgentDesktopStatus {
  return {
    installed: true,
    version: '0.4.0',
    pinned: '0.4.0',
    path: '/opt/agent-desktop',
    driver_active: 'agent-desktop',
    permissions: { accessibility: 'granted', screen_recording: 'granted' },
    job: { state: 'idle', log_tail: '', error: null },
    ...overrides,
  }
}

describe('versionState / installAction', () => {
  it('maps every install shape to one state and one action', () => {
    expect(versionState(status({ installed: false, version: null }))).toBe('missing')
    expect(installAction(status({ installed: false, version: null }))).toBe('install')
    expect(versionState(status())).toBe('current')
    expect(installAction(status())).toBeNull()
    expect(versionState(status({ version: '0.3.9' }))).toBe('outdated')
    expect(installAction(status({ version: '0.3.9' }))).toBe('update')
    expect(versionState(status({ version: null }))).toBe('unknown')
    expect(installAction(status({ version: null }))).toBe('reinstall')
  })
})

describe('jobRunning / statusPollInterval', () => {
  it('polls only while the gateway runs a job or this tab waits on its POST', () => {
    expect(statusPollInterval(status(), false)).toBe(false)
    expect(statusPollInterval(undefined, false)).toBe(false)
    expect(statusPollInterval(undefined, true)).toBe(AGENT_DESKTOP_POLL_MS)
    const running = status({ job: { state: 'running', log_tail: 'x', error: null } })
    expect(jobRunning(running, false)).toBe(true)
    expect(statusPollInterval(running, false)).toBe(AGENT_DESKTOP_POLL_MS)
    expect(statusPollInterval(status({ job: { state: 'done', log_tail: '', error: null } }), false)).toBe(false)
  })
})

describe('failedJob', () => {
  it('returns the failure only when no new job is running', () => {
    const failed = status({ job: { state: 'failed', log_tail: 'npm ERR!', error: 'exit 1' } })
    expect(failedJob(failed, false)).toEqual({ error: 'exit 1', logTail: 'npm ERR!' })
    expect(failedJob(failed, true)).toBeNull()
    expect(failedJob(status(), false)).toBeNull()
  })
})

describe('permissions', () => {
  it('flags any grant that is not granted', () => {
    expect(permissionsIncomplete(status())).toBe(false)
    expect(
      permissionsIncomplete(status({ permissions: { accessibility: 'granted', screen_recording: 'unknown' } })),
    ).toBe(true)
    expect(permissionVariant('granted')).toBe('ok')
    expect(permissionVariant('denied')).toBe('warn')
    expect(permissionVariant('unknown')).toBe('muted')
    expect(permissionVariant(undefined)).toBe('muted')
  })
})
