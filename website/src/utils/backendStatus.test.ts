import { describe, it, expect } from 'vitest'

import { restartExhaustedNotice } from './backendStatus'

describe('restartExhaustedNotice', () => {
  it('returns null when the backend is not restart-exhausted', () => {
    expect(restartExhaustedNotice('Dev Fleet', undefined)).toBeNull()
    expect(restartExhaustedNotice('Dev Fleet', null)).toBeNull()
    expect(restartExhaustedNotice('Dev Fleet', { running: false })).toBeNull()
    expect(
      restartExhaustedNotice('Dev Fleet', { restart_exhausted: null }),
    ).toBeNull()
  })

  it('names the app, the count, and the disable/re-enable action', () => {
    const notice = restartExhaustedNotice('Dev Fleet', {
      restart_exhausted: { attempts: 8 },
    })
    expect(notice).not.toBeNull()
    expect(notice!.title).toBe('Dev Fleet keeps failing to restart')
    expect(notice!.message).toContain('Dev Fleet')
    expect(notice!.message).toContain('8')
    expect(notice!.message).toContain('disable and re-enable')
  })

  it('uses the singular message for one attempt', () => {
    const notice = restartExhaustedNotice('My App', {
      restart_exhausted: { attempts: 1 },
    })
    expect(notice!.message).toContain('1 time in a row')
  })

  it('uses the plural message for more than one attempt', () => {
    const notice = restartExhaustedNotice('My App', {
      restart_exhausted: { attempts: 5 },
    })
    expect(notice!.message).toContain('5 times in a row')
  })

  it('falls back to a sensible count for a malformed attempts value', () => {
    for (const bad of [0, -3, Number.NaN, Number.POSITIVE_INFINITY]) {
      const notice = restartExhaustedNotice('App', {
        restart_exhausted: { attempts: bad as number },
      })
      expect(notice).not.toBeNull()
      expect(notice!.message).toContain('1 time in a row')
    }
  })
})
