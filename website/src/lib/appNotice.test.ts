import { describe, expect, it } from 'vitest'
import { store } from '../store'
import { publishAppNotice } from './appNotice'

describe('publishAppNotice', () => {
  it('adds an app-wide notification for an ownerless failure', () => {
    publishAppNotice('popup blocked', 'error')
    expect(store.getState().notifications.items.at(-1)).toEqual(
      expect.objectContaining({ kind: 'error', title: 'popup blocked', body: '' }),
    )
  })
})
