/**
 * The provision step of a cloud launch shows how long it has run, in the launch
 * card's step list and in the "Setting up" row, and a built-in job says once it
 * is past its 6-minute estimate. Time is pinned with `vi.setSystemTime`; the
 * expected text comes from the catalog and `fmtElapsed`, never a literal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { LaunchProgressCard, SettingUpRow } from '../pages/settings/RemoteCrewPanelLaunch'
import { __resetSecondClockForTests } from '../hooks/useSecondClock'
import { fmtElapsed } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { BUILTIN_PROVISIONER_ID } from '../utils/remoteCrew'
import type { LaunchJob } from '../api/client'

const NOW_MS = new Date('2026-11-13T12:00:00Z').getTime()

function job(launchedSecondsAgo: number, provider_id = BUILTIN_PROVISIONER_ID): LaunchJob {
  return {
    id: 'job-1',
    provider_id,
    profile: 'dev',
    region: 'us-east-1',
    size_key: 'balanced',
    tag: 'kc-abc123',
    status: 'running',
    steps: [
      { key: 'preflight', label: 'Check your AWS setup', state: 'done' },
      { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'active', detail: 'Creating the instance…' },
      { key: 'signin', label: 'Sign in to Kiro', state: 'pending' },
      { key: 'connect', label: 'Connect', state: 'pending' },
    ],
    created_at: NOW_MS / 1000 - launchedSecondsAgo,
    updated_at: NOW_MS / 1000,
  }
}

const plain = (seconds: number) =>
  i18nT('pages.settings.remoteCrewPanel.provision_elapsed', { elapsed: fmtElapsed(seconds * 1000, { wholeSeconds: true }) })
const overrun = (seconds: number) =>
  i18nT('pages.settings.remoteCrewPanel.provision_past_estimate', { elapsed: fmtElapsed(seconds * 1000, { wholeSeconds: true }) })

function renderBoth(j: LaunchJob) {
  renderWithProviders(
    <>
      <LaunchProgressCard
        job={j}
        onCancel={() => {}}
        onRestartSignin={() => {}}
        restartingSignin={false}
        onFetchSignin={() => {}}
        fetchingSignin={false}
        signinNotice={null}
        cancelling={false}
      />
      <SettingUpRow job={j} onCancel={() => {}} cancelling={false} />
    </>,
  )
  return screen.getAllByTestId('provision-elapsed').map(el => el.textContent)
}

describe('provision step elapsed time', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    __resetSecondClockForTests()
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetSecondClockForTests()
  })

  it('shows the time since Launch on the card and the row', () => {
    expect(renderBoth(job(30))).toEqual([plain(30), plain(30)])
    expect(plain(30)).toContain(fmtElapsed(30_000, { wholeSeconds: true }))
  })

  it('says a built-in job is past its 6-minute estimate', () => {
    expect(renderBoth(job(7 * 60))).toEqual([overrun(7 * 60), overrun(7 * 60)])
  })

  it('keeps the plain line for a provisioner that made no estimate', () => {
    expect(renderBoth(job(7 * 60, 'some_edition_lane'))).toEqual([plain(7 * 60), plain(7 * 60)])
  })

  it('reads zero, not negative time, when the gateway clock is ahead', () => {
    expect(renderBoth(job(-5))).toEqual([plain(0), plain(0)])
  })

  it('shows nothing once the provision step is no longer running', () => {
    const j = job(30)
    j.steps = j.steps.map(s => (s.key === 'provision' ? { ...s, state: 'done' } : s.key === 'signin' ? { ...s, state: 'active' } : s))
    renderWithProviders(<SettingUpRow job={j} onCancel={() => {}} cancelling={false} />)
    expect(screen.queryByTestId('provision-elapsed')).toBeNull()
  })
})
