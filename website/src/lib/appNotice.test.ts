import { describe, expect, it } from 'vitest'
import { store } from '../store'
import { appErrorNotice } from './appNotice'
import { parseTs } from '../components/notifications/notifMeta'

describe('appNotice', () => {
  it('adds an app-wide notification for an ownerless failure', () => {
    appErrorNotice('popup blocked')
    expect(store.getState().notifications.items.at(-1)).toEqual(
      expect.objectContaining({ kind: 'error', title: 'popup blocked', body: '' }),
    )
  })

  it('stamps a ts the bell feed can parse as a time', () => {
    appErrorNotice('first')
    appErrorNotice('second')
    const [a, b] = store.getState().notifications.items.slice(-2)
    // A `<ms>-<n>` stamp parses as Invalid Date, which the feed renders as "NaNd ago".
    expect(Number.isNaN(parseTs(a.ts).getTime())).toBe(false)
    expect(Number.isNaN(parseTs(b.ts).getTime())).toBe(false)
    // Two notices in one millisecond must not dedupe into one.
    expect(a.ts).not.toBe(b.ts)
  })
})
