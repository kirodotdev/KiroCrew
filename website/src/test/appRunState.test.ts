import { describe, expect, it } from 'vitest'

import { SUCCESS_WINDOW_MS, appRunStates, nextSuccessExpiryMs } from '../appRunState'
import type { CronJob } from '../types'

/** A job carrying only the fields the derivation reads. */
function job(over: Partial<CronJob>): CronJob {
  return {
    id: 'j', name: 'j', message: '', enabled: true, schedule: '', last_status: '',
    ...over,
  } as CronJob
}

const NOW = 1_700_000_000_000
/** Epoch SECONDS, the unit the wire carries, for a run *agoMs* ago. */
const ranAgo = (agoMs: number) => (NOW - agoMs) / 1000

describe('appRunStates', () => {
  it('reports a running app-owned job', () => {
    expect(appRunStates([job({ app: 'ledger', is_running: true })], NOW)).toEqual({
      ledger: 'running',
    })
  })

  it('reports an errored app-owned job', () => {
    expect(appRunStates([job({ app: 'ledger', last_status: 'error' })], NOW)).toEqual({
      ledger: 'error',
    })
  })

  it('ignores a job owned by a person, not an app', () => {
    // `app` absent is the person-owned shape the host produces.
    expect(appRunStates([job({ last_status: 'error' })], NOW)).toEqual({})
    expect(appRunStates([job({ app: null, is_running: true })], NOW)).toEqual({})
  })

  it('omits an app whose job has never run', () => {
    // No last_status and not running: "ready" is not a state worth a mark.
    expect(appRunStates([job({ app: 'ledger' })], NOW)).toEqual({})
  })

  it('omits an app entirely rather than reporting an idle value', () => {
    // Absence is the contract: a caller merging maps must be able to tell
    // "nothing to say" from "says idle".
    const states = appRunStates([job({ app: 'ledger' })], NOW)
    expect('ledger' in states).toBe(false)
  })

  describe('the success window', () => {
    it('reports success for a run inside the window', () => {
      const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(1_000) })
      expect(appRunStates([j], NOW)).toEqual({ ledger: 'success' })
    })

    it('drops success once the window has passed', () => {
      const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(SUCCESS_WINDOW_MS + 1_000) })
      expect(appRunStates([j], NOW)).toEqual({})
    })

    it('drops success for an ok job with no run timestamp', () => {
      // Unplaceable in or out of the window, so it must not become a mark that
      // never expires.
      expect(appRunStates([job({ app: 'ledger', last_status: 'ok' })], NOW)).toEqual({})
    })

    it('treats a future timestamp as fresh rather than expired', () => {
      // Host/browser clock skew, not a stale run.
      const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(-5_000) })
      expect(appRunStates([j], NOW)).toEqual({ ledger: 'success' })
    })
  })

  describe('a user-paused job is not a health signal, but an auto-paused one is', () => {
    it('ignores a job the USER paused', () => {
      // Mirrors cron.py's unhealthy_jobs_from_disk, which skips a user-paused
      // record: pausing is the opt-out that works with the app's page closed.
      expect(appRunStates([
        job({ app: 'ledger', enabled: false, user_paused: true, last_status: 'error' }),
      ], NOW)).toEqual({})
    })

    it('REPORTS a job execution auto-paused after repeated failures', () => {
      // The bug this covers: auto-pause sets enabled=False exactly like a user
      // pause (cron.py record_failure), so reading `enabled` alone dropped the
      // mark on an app's WORST job -- the one that failed enough times for the
      // scheduler to give up on it. The wire distinguishes them with
      // `user_paused`, which execution never sets.
      expect(appRunStates([
        job({ app: 'ledger', enabled: false, user_paused: false, last_status: 'error' }),
      ], NOW)).toEqual({ ledger: 'error' })
    })

    it('falls back to `enabled` when the gateway omits user_paused', () => {
      // An older gateway serves no `user_paused`; the previous behaviour is then
      // the safe reading rather than treating every disabled job as a signal.
      expect(appRunStates([
        job({ app: 'ledger', enabled: false, last_status: 'error' }),
      ], NOW)).toEqual({})
    })

    it('still reports a disabled job that is running NOW', () => {
      // `enabled` governs the NEXT run, not whether this one is real; hiding it
      // would contradict the Schedule page.
      const j = job({ app: 'ledger', enabled: false, is_running: true })
      expect(appRunStates([j], NOW)).toEqual({ ledger: 'running' })
    })
  })

  describe('collapsing several jobs of one app', () => {
    it('ranks running above error', () => {
      // Mirrors SchedulePage's status cell, which tests is_running BEFORE
      // last_status. A still-true error reappears when the run finishes.
      const jobs = [
        job({ id: 'a', app: 'ledger', last_status: 'error' }),
        job({ id: 'b', app: 'ledger', is_running: true }),
      ]
      expect(appRunStates(jobs, NOW)).toEqual({ ledger: 'running' })
    })

    it('ranks error above a fresh success', () => {
      const jobs = [
        job({ id: 'a', app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(1_000) }),
        job({ id: 'b', app: 'ledger', last_status: 'error' }),
      ]
      expect(appRunStates(jobs, NOW)).toEqual({ ledger: 'error' })
    })

    it('keeps two apps independent', () => {
      const jobs = [
        job({ id: 'a', app: 'ledger', is_running: true }),
        job({ id: 'b', app: 'radar', last_status: 'error' }),
      ]
      expect(appRunStates(jobs, NOW)).toEqual({ ledger: 'running', radar: 'error' })
    })
  })

  describe('an app name that collides with Object.prototype', () => {
    // An app name is attacker-chosen and the manifest reserves only a namespace
    // list, so these are all valid names.
    it('does not read an inherited member for an absent app', () => {
      const states = appRunStates([job({ app: 'ledger', is_running: true })], NOW)
      expect(states['constructor']).toBeUndefined()
      expect(states['toString']).toBeUndefined()
      expect(states['__proto__']).toBeUndefined()
    })

    it('stores such a name as an ordinary key', () => {
      const states = appRunStates([job({ app: 'constructor', last_status: 'error' })], NOW)
      expect(states['constructor']).toBe('error')
    })
  })

  it('ignores a bare app name of empty string', () => {
    // An empty key matches no rail row and would silently accumulate.
    expect(appRunStates([job({ app: '', is_running: true })], NOW)).toEqual({})
  })
})

describe('nextSuccessExpiryMs', () => {
  it('returns null when nothing is showing success', () => {
    // Null is what lets a caller arm no timer at all in the common case.
    expect(nextSuccessExpiryMs([job({ app: 'ledger', is_running: true })], NOW)).toBeNull()
    expect(nextSuccessExpiryMs([job({ app: 'ledger', last_status: 'error' })], NOW)).toBeNull()
    expect(nextSuccessExpiryMs([], NOW)).toBeNull()
  })

  it('returns the remaining window for a showing success', () => {
    const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(10_000) })
    expect(nextSuccessExpiryMs([j], NOW)).toBe(SUCCESS_WINDOW_MS - 10_000)
  })

  it('returns null once the window has already passed', () => {
    // An expired run shows no mark, so there is nothing to wait for.
    const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(SUCCESS_WINDOW_MS + 1) })
    expect(nextSuccessExpiryMs([j], NOW)).toBeNull()
  })

  it('takes the EARLIEST expiry when several successes are showing', () => {
    const jobs = [
      job({ id: 'a', app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(10_000) }),
      job({ id: 'b', app: 'radar', last_status: 'ok', last_run_ts: ranAgo(80_000) }),
    ]
    // The later run expires last; the rail must clear the first one on time.
    expect(nextSuccessExpiryMs(jobs, NOW)).toBe(SUCCESS_WINDOW_MS - 80_000)
  })

  it('waits the full window from a future timestamp rather than going negative', () => {
    // Clock skew: `jobState` admits a future run as fresh, so its mark is due to
    // clear 90s after THAT moment -- window + skew from now, not less than the
    // window. The clamp guards the lower bound only.
    const skewMs = 5_000
    const j = job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(-skewMs) })
    expect(nextSuccessExpiryMs([j], NOW)).toBe(SUCCESS_WINDOW_MS + skewMs)
  })

  it('ignores a paused job and a job owned by a person', () => {
    expect(nextSuccessExpiryMs([
      job({ app: 'ledger', enabled: false, last_status: 'ok', last_run_ts: ranAgo(1_000) }),
      job({ last_status: 'ok', last_run_ts: ranAgo(1_000) }),
    ], NOW)).toBeNull()
  })

  it('arms no timer for a success the app does not SHOW', () => {
    // The app's winning state is error, so the recent success is already hidden
    // by rank. Arming on it would fire a timer whose expiry changes no pixel.
    expect(nextSuccessExpiryMs([
      job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(1_000) }),
      job({ app: 'ledger', last_status: 'error' }),
    ], NOW)).toBeNull()
  })

  it('still arms for another app that DOES show its success', () => {
    // The suppression is per app, not global: one app's error must not silence
    // another app's expiring mark.
    expect(nextSuccessExpiryMs([
      job({ app: 'ledger', last_status: 'ok', last_run_ts: ranAgo(1_000) }),
      job({ app: 'ledger', last_status: 'error' }),
      job({ app: 'notes', last_status: 'ok', last_run_ts: ranAgo(5_000) }),
    ], NOW)).toBe(SUCCESS_WINDOW_MS - 5_000)
  })
})
