import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import {
  clearFeatureNewTag, deliverFeatureNewTag, featureNewTagOriginOf, featureNewTagRoute, landFeatureNewTag,
  takeFeatureNewTagFlight, useFeatureNewTag,
} from './featureNewTag'

const origin = { left: 300, top: 200, width: 28, height: 28, src: 'data:image/svg+xml,mate' }

/** A Mates rail row that is laid out, so there is somewhere to fly to. */
function railRow() {
  const row = document.createElement('div')
  row.setAttribute('data-onboarding-nav', 'members')
  row.getClientRects = () => [new DOMRect(0, 0, 40, 20)] as unknown as DOMRectList
  document.body.appendChild(row)
  return row
}

const matchMedia = window.matchMedia
function reduceMotion(matches: boolean) {
  window.matchMedia = ((query: string) => ({ ...matchMedia(query), matches })) as typeof window.matchMedia
}

beforeEach(() => {
  localStorage.clear()
  act(() => clearFeatureNewTag('members'))
  // Drain a flight a previous test left waiting.
  takeFeatureNewTagFlight('members')
  document.body.replaceChildren()
})

afterEach(() => {
  window.matchMedia = matchMedia
  vi.useRealTimers()
})

describe('featureNewTag', () => {
  it('keeps the intro route with the tag until the tag clears', () => {
    act(() => deliverFeatureNewTag('members', null, '/members?member=mate'))
    expect(featureNewTagRoute('members')).toBe('/members?member=mate')
    act(() => clearFeatureNewTag('members'))
    expect(featureNewTagRoute('members')).toBeNull()
  })

  it('refuses a stored route that does not start at the dashboard root', () => {
    localStorage.setItem('mc-feature-new-tag-routes', JSON.stringify({ members: 'https://evil.example/' }))
    expect(featureNewTagRoute('members')).toBeNull()
  })

  it('shows the tag at once when there is no avatar to fly from', () => {
    railRow()
    const { result } = renderHook(() => useFeatureNewTag('members'))
    expect(result.current).toBeNull()
    act(() => deliverFeatureNewTag('members', null))
    expect(result.current).toBe('shown')
    expect(takeFeatureNewTagFlight('members')).toBeNull()
  })

  it('shows the tag at once when the rail row is not on screen', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', origin))
    expect(result.current).toBe('shown')
    expect(takeFeatureNewTagFlight('members')).toBeNull()
  })

  it('shows the tag at once under reduced motion, with no flight', () => {
    railRow()
    reduceMotion(true)
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', origin))
    expect(result.current).toBe('shown')
    expect(takeFeatureNewTagFlight('members')).toBeNull()
  })

  it('stores the tag as arriving before the flight, and the row claims the flight once', () => {
    railRow()
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', origin, '/members?member=mate'))
    // The tag is already given -- stored, and the route with it -- before any
    // flyer has moved.
    expect(result.current).toBe('arriving')
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({ members: 'arriving' })
    expect(featureNewTagRoute('members')).toBe('/members?member=mate')
    expect(takeFeatureNewTagFlight('members')).toEqual(origin)
    expect(takeFeatureNewTagFlight('members')).toBeNull()
    act(() => landFeatureNewTag('members'))
    expect(result.current).toBe('shown')
  })

  it('shows the tag by the next frame when no row picks the flight up', () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame'] })
    railRow()
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', origin))
    expect(result.current).toBe('arriving')
    act(() => { vi.runAllTimers() })
    expect(result.current).toBe('shown')
    expect(takeFeatureNewTagFlight('members')).toBeNull()
  })

  it('does not bring back a tag cleared mid-flight', () => {
    railRow()
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', origin))
    takeFeatureNewTagFlight('members')
    act(() => clearFeatureNewTag('members'))
    act(() => landFeatureNewTag('members'))
    expect(result.current).toBeNull()
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({})
  })

  it('follows another tab, and reads a mid-flight reload or an older build\'s value as shown', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    const junk = renderHook(() => useFeatureNewTag('junk'))
    act(() => {
      localStorage.setItem('mc-feature-new-tags', JSON.stringify({ members: 'arriving', junk: 0 }))
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-feature-new-tags' }))
    })
    expect(result.current).toBe('shown')
    expect(junk.result.current).toBeNull()
  })

  it('clears for good', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', null))
    act(() => clearFeatureNewTag('members'))
    expect(result.current).toBeNull()
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({})
  })

  it('survives a corrupt stored value', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => {
      localStorage.setItem('mc-feature-new-tags', '{not json')
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-feature-new-tags' }))
    })
    expect(result.current).toBeNull()
  })
})

describe('featureNewTagOriginOf', () => {
  it('measures a laid-out avatar and keeps its picture', () => {
    const img = document.createElement('img')
    img.src = 'data:image/svg+xml,mate'
    img.getClientRects = () => [new DOMRect(300, 200, 28, 28)] as unknown as DOMRectList
    img.getBoundingClientRect = () => new DOMRect(300, 200, 28, 28)
    expect(featureNewTagOriginOf(img)).toEqual(origin)
  })

  it('is null for no avatar, or one that is not laid out', () => {
    expect(featureNewTagOriginOf(null)).toBeNull()
    const img = document.createElement('img')
    img.getClientRects = () => [] as unknown as DOMRectList
    expect(featureNewTagOriginOf(img)).toBeNull()
  })
})
