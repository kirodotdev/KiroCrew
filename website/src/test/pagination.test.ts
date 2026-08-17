// Feature: chat-older-history
// The whole gate: provenance and scrollability checks were removed, not relocated.
import { describe, it, expect } from 'vitest'
import { shouldPaginateOlder } from '../pages/chat/pagination'

describe('shouldPaginateOlder', () => {
  it('paginates when the server reported more history and nothing is in flight', () => {
    expect(shouldPaginateOlder({ loadingOlder: false, slotHasMore: true })).toBe(true)
  })

  it('does not paginate while a fetch is already in flight', () => {
    expect(shouldPaginateOlder({ loadingOlder: true, slotHasMore: true })).toBe(false)
  })

  it('does not paginate when the server reported no more history', () => {
    expect(shouldPaginateOlder({ loadingOlder: false, slotHasMore: false })).toBe(false)
  })

  it('an in-flight fetch outranks unloaded history', () => {
    expect(shouldPaginateOlder({ loadingOlder: true, slotHasMore: false })).toBe(false)
  })
})
