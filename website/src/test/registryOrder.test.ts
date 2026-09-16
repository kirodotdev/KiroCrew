import { describe, it, expect } from 'vitest'
import { orderByReview, reviewRank } from '../components/appstore/registryOrder'

describe('reviewRank', () => {
  it('puts curated first and community last', () => {
    expect(reviewRank('curated')).toBeLessThan(reviewRank(''))
    expect(reviewRank('')).toBeLessThan(reviewRank('community'))
  })

  it('ranks an absent tier with the unreviewed middle', () => {
    expect(reviewRank(undefined)).toBe(reviewRank(''))
  })

  it('ranks an UNKNOWN tier with the unreviewed middle, claiming nothing', () => {
    // A future core could add a tier this build does not know. Guessing it into
    // the curated group would advertise a review that may never have happened;
    // guessing it into community would defame the source.
    expect(reviewRank('platinum')).toBe(reviewRank(''))
  })
})

describe('orderByReview', () => {
  it('lifts a curated row above a community row listed first', () => {
    const rows = [{ name: 'community', review: 'community' }, { name: 'internal', review: 'curated' }]
    expect(orderByReview(rows).map(r => r.name)).toEqual(['internal', 'community'])
  })

  it('is stable within a tier, so today\'s order survives', () => {
    const rows = [
      { name: 'b', review: '' },
      { name: 'a', review: '' },
      { name: 'c' },
    ]
    expect(orderByReview(rows).map(r => r.name)).toEqual(['b', 'a', 'c'])
  })

  it('does not mutate the input array', () => {
    // The input is normally a React Query cache array; sorting it in place would
    // reorder the cached value every other consumer reads.
    const rows = [{ name: 'community', review: 'community' }, { name: 'internal', review: 'curated' }]
    orderByReview(rows)
    expect(rows.map(r => r.name)).toEqual(['community', 'internal'])
  })

  it('orders all three tiers curated, unreviewed, community', () => {
    const rows = [
      { name: 'community', review: 'community' },
      { name: 'plain', review: '' },
      { name: 'curated', review: 'curated' },
    ]
    expect(orderByReview(rows).map(r => r.name)).toEqual(['curated', 'plain', 'community'])
  })
})
