import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import { asWorkInFlight, WorkConfirmDeclined, workInFlightNotice } from './workInFlight'

describe('asWorkInFlight', () => {
  it('reads an update deferral', () => {
    expect(asWorkInFlight({ status: 'deferred', busy: 3, quiet_remaining_secs: 120, retry_after_secs: 45 }))
      .toEqual({ busy: 3, quietRemainingSecs: 120, holders: [], holdersMore: 0 })
  })

  it('reads a restart refusal with the same counts', () => {
    expect(asWorkInFlight({ code: 'work_in_flight', busy: 1 }))
      .toEqual({ busy: 1, quietRemainingSecs: 0, holders: [], holdersMore: 0 })
  })

  it('takes the longer of the typing wait and the settle wait', () => {
    expect(asWorkInFlight({ status: 'deferred', quiet_remaining_secs: 5, settle_remaining_secs: 40 })?.quietRemainingSecs)
      .toBe(40)
  })

  it('falls back to whole numbers on a malformed answer', () => {
    expect(asWorkInFlight({ status: 'deferred', busy: 'x', quiet_remaining_secs: -1 }))
      .toEqual({ busy: 0, quietRemainingSecs: 0, holders: [], holdersMore: 0 })
  })

  it('reads the holders a restart refusal names, and skips an entry it cannot read', () => {
    const w = asWorkInFlight({
      code: 'work_in_flight',
      busy: 4,
      holders: [
        { kind: 'script', name: 'nightly-sync', since_secs: 21.7, count: 1 },
        { kind: 'subagent', name: '', since_secs: null, count: 3 },
        { name: 'no kind' },
        'chat-1',
        { kind: 'turn', name: 7, since_secs: -1, count: 0 },
      ],
      holders_more: 2,
    })
    expect(w?.holders).toEqual([
      { kind: 'script', name: 'nightly-sync', sinceSecs: 21, count: 1 },
      { kind: 'subagent', name: '', sinceSecs: null, count: 3 },
      { kind: 'turn', name: '', sinceSecs: null, count: 1 },
    ])
    expect(w?.holdersMore).toBe(2)
  })

  it.each([null, undefined, 'deferred', 3, { status: 'updating' }, { code: 'restart_pending' }, {}])('is null for %j', (answer) => {
    expect(asWorkInFlight(answer)).toBeNull()
  })
})

describe('workInFlightNotice', () => {
  it('names the active items while work is running', () => {
    expect(workInFlightNotice({ busy: 2, quietRemainingSecs: 600, holders: [], holdersMore: 0 }))
      .toBe(i18nT('components.updateFoundModal.work_running_busy', { active: 2 }))
  })

  it('lists the holders under the count: name, kind and age, an unnamed count, then how many more', () => {
    const notice = workInFlightNotice({
      busy: 9,
      quietRemainingSecs: 0,
      holders: [
        { kind: 'script', name: 'nightly-sync', sinceSecs: 21, count: 1 },
        { kind: 'turn', name: 'chat-95', sinceSecs: null, count: 1 },
        { kind: 'subagent', name: '', sinceSecs: null, count: 3 },
        { kind: 'app_update', name: '', sinceSecs: null, count: 1 },
      ],
      holdersMore: 3,
    })
    expect(notice).toBe(
      'Restarting now would interrupt running work. Active items: 9.\nRunning now: '
        + 'nightly-sync (scheduled job, 21s), chat-95 (chat turn), sub-agent ×3, other work, and 3 more.',
    )
  })

  it('says only that a source could not be checked, never its internal label', () => {
    const notice = workInFlightNotice({
      busy: 1,
      quietRemainingSecs: 0,
      holders: [{ kind: 'unreadable', name: 'slot queue', sinceSecs: null, count: 1 }],
      holdersMore: 0,
    })
    expect(notice).toContain(i18nT('components.updateFoundModal.work_kind_unreadable'))
    expect(notice).not.toContain('slot queue')
  })

  it('names the minutes still owed once nothing is running, never fewer than one', () => {
    expect(workInFlightNotice({ busy: 0, quietRemainingSecs: 61, holders: [], holdersMore: 0 }))
      .toBe(i18nT('components.updateFoundModal.work_running_quiet', { minutes: 2 }))
    expect(workInFlightNotice({ busy: 0, quietRemainingSecs: 0, holders: [], holdersMore: 0 }))
      .toBe(i18nT('components.updateFoundModal.work_running_quiet', { minutes: 1 }))
  })
})

describe('WorkConfirmDeclined', () => {
  it('is an ApiError, so a surface that shows a refusal shows this one', () => {
    const e = new WorkConfirmDeclined('Restart cancelled.')
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(409)
    expect(e.message).toBe('Restart cancelled.')
  })
})
