import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import { asWorkInFlight, WorkConfirmDeclined, workInFlightNotice } from './workInFlight'

describe('asWorkInFlight', () => {
  it('reads an update deferral', () => {
    expect(asWorkInFlight({ status: 'deferred', busy: 3, quiet_remaining_secs: 120, retry_after_secs: 45 }))
      .toEqual({ busy: 3, quietRemainingSecs: 120 })
  })

  it('reads a restart refusal with the same counts', () => {
    expect(asWorkInFlight({ code: 'work_in_flight', busy: 1 })).toEqual({ busy: 1, quietRemainingSecs: 0 })
  })

  it('takes the longer of the typing wait and the settle wait', () => {
    expect(asWorkInFlight({ status: 'deferred', quiet_remaining_secs: 5, settle_remaining_secs: 40 })?.quietRemainingSecs)
      .toBe(40)
  })

  it('falls back to whole numbers on a malformed answer', () => {
    expect(asWorkInFlight({ status: 'deferred', busy: 'x', quiet_remaining_secs: -1 }))
      .toEqual({ busy: 0, quietRemainingSecs: 0 })
  })

  it.each([null, undefined, 'deferred', 3, { status: 'updating' }, { code: 'restart_pending' }, {}])('is null for %j', (answer) => {
    expect(asWorkInFlight(answer)).toBeNull()
  })
})

describe('workInFlightNotice', () => {
  it('names the active items while work is running', () => {
    expect(workInFlightNotice({ busy: 2, quietRemainingSecs: 600 }))
      .toBe(i18nT('components.updateFoundModal.work_running_busy', { active: 2 }))
  })

  it('names the minutes still owed once nothing is running, never fewer than one', () => {
    expect(workInFlightNotice({ busy: 0, quietRemainingSecs: 61 }))
      .toBe(i18nT('components.updateFoundModal.work_running_quiet', { minutes: 2 }))
    expect(workInFlightNotice({ busy: 0, quietRemainingSecs: 0 }))
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
