import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { clearFeatureNewTag, deliverFeatureNewTag, FEATURE_LANDED_EVENT, featureNewTagRoute, landFeatureGhost, useFeatureNewTag } from './featureNewTag'

const origin = { cx: 100, top: 100, height: 60, popIn: false }

beforeEach(() => {
  localStorage.clear()
  act(() => clearFeatureNewTag('members'))
  document.body.replaceChildren()
})

describe('featureNewTag', () => {
  it('keeps the intro route with the tag until the tag clears', () => {
    act(() => deliverFeatureNewTag('members', 'New', null, '/members?member=default'))
    expect(featureNewTagRoute('members')).toBe('/members?member=default')
    act(() => clearFeatureNewTag('members'))
    expect(featureNewTagRoute('members')).toBeNull()
  })

  it('shows the tag at once when the rail row is not on screen', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    expect(result.current).toBeNull()
    act(() => deliverFeatureNewTag('members', 'New', origin))
    expect(result.current).toBe('shown')
  })

  it('shows the tag at once when there is no origin to fly from', () => {
    const row = document.createElement('div')
    row.setAttribute('data-onboarding-nav', 'members')
    document.body.appendChild(row)
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', 'New', null))
    expect(result.current).toBe('shown')
  })

  it('follows another tab, and reads a mid-flight reload as shown', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    const junk = renderHook(() => useFeatureNewTag('junk'))
    act(() => {
      localStorage.setItem('mc-feature-new-tags', JSON.stringify({ members: 'arriving', junk: 7 }))
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-feature-new-tags' }))
    })
    expect(result.current).toBe('shown')
    expect(junk.result.current).toBeNull()
  })

  it('clears for good', () => {
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', 'New', origin))
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

describe('landFeatureGhost', () => {
  it('fires the landed event with no flight when there is no origin to fly from', async () => {
    const anchor = document.createElement('span')
    anchor.setAttribute('data-feature-landing', 'members')
    anchor.getClientRects = () => [new DOMRect(0, 0, 36, 36)] as unknown as DOMRectList
    document.body.appendChild(anchor)
    const landed = new Promise<string>(resolve => {
      window.addEventListener(FEATURE_LANDED_EVENT, e => resolve((e as CustomEvent<{ navId: string }>).detail.navId), { once: true })
    })
    landFeatureGhost('members', null)
    expect(await landed).toBe('members')
    expect(document.body.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0)
  })

  // jsdom has no Web Animations; each stub animation finishes when told to.
  function stubAnimate() {
    const running: { finish: () => void }[] = []
    const original = Element.prototype.animate
    Element.prototype.animate = function () {
      // Finishes once, as a real animation does: finishing again is a no-op.
      const a = { done: false, onfinish: null as (() => void) | null, finish() { if (!a.done) { a.done = true; a.onfinish?.() } } }
      running.push(a)
      return a as unknown as Animation
    }
    return {
      finishAll: () => [...running].forEach(a => a.finish()),
      restore: () => { Element.prototype.animate = original },
    }
  }

  async function flyTo(anchor: HTMLElement) {
    anchor.setAttribute('data-feature-landing', 'members')
    anchor.getClientRects = () => [new DOMRect(0, 0, 36, 36)] as unknown as DOMRectList
    anchor.appendChild(document.createElement('img'))
    document.body.appendChild(anchor)
    landFeatureGhost('members', origin)
    await new Promise(r => requestAnimationFrame(() => r(null)))
  }

  it('keeps a ghost avatar tile empty until impact', async () => {
    const anim = stubAnimate()
    try {
      const anchor = document.createElement('span')
      anchor.setAttribute('data-feature-landing-tile', '#8c9a2b')
      await flyTo(anchor)
      const covers = () => [...document.body.querySelectorAll<HTMLElement>('div[aria-hidden="true"]')]
        .filter(el => el.style.backgroundColor === '#8c9a2b')
      expect(covers()).toHaveLength(1)
      expect(anchor.style.visibility).toBe('')
      anim.finishAll()
      expect(covers()).toHaveLength(0)
    } finally {
      anim.restore()
    }
  })

  it('lands on the visible copy when the page renders the spot twice', async () => {
    const hidden = document.createElement('span')
    hidden.setAttribute('data-feature-landing', 'members')
    hidden.getClientRects = () => [] as unknown as DOMRectList
    document.body.appendChild(hidden)
    const anim = stubAnimate()
    try {
      const shown = document.createElement('span')
      await flyTo(shown)
      expect(shown.style.visibility).toBe('hidden')
      expect(hidden.style.visibility).toBe('')
      anim.finishAll()
    } finally {
      anim.restore()
    }
  })

  it('hides a picture avatar, which has no tile to show, until impact', async () => {
    const anim = stubAnimate()
    try {
      const anchor = document.createElement('span')
      await flyTo(anchor)
      expect(anchor.style.visibility).toBe('hidden')
      anim.finishAll()
      expect(anchor.style.visibility).toBe('')
    } finally {
      anim.restore()
    }
  })
})

describe('deliverFeatureNewTag flight', () => {
  // jsdom has no Web Animations; each stub finishes when told to.
  function stubAnimate() {
    const running: { finish: () => void }[] = []
    const original = Element.prototype.animate
    Element.prototype.animate = function () {
      // Finishes once, as a real animation does: finishing again is a no-op.
      const a = { done: false, onfinish: null as (() => void) | null, finish() { if (!a.done) { a.done = true; a.onfinish?.() } } }
      running.push(a)
      return a as unknown as Animation
    }
    return {
      finishAll: () => [...running].forEach(a => a.finish()),
      restore: () => { Element.prototype.animate = original },
    }
  }
  const frame = () => new Promise(r => requestAnimationFrame(() => r(null)))
  const visible = (el: HTMLElement) => {
    el.getClientRects = () => [new DOMRect(0, 0, 40, 20)] as unknown as DOMRectList
    document.body.appendChild(el)
    return el
  }

  function railWithPill() {
    const row = document.createElement('div')
    row.setAttribute('data-onboarding-nav', 'members')
    visible(row)
    const pill = document.createElement('span')
    pill.setAttribute('data-feature-new-tag', 'members')
    document.body.appendChild(pill)
  }

  for (const popIn of [false, true]) {
    it(`flies the ghost to the pill, shows the tag, then removes the courier${popIn ? ' (pop-in start)' : ''}`, async () => {
      railWithPill()
      const anim = stubAnimate()
      try {
        const { result } = renderHook(() => useFeatureNewTag('members'))
        act(() => deliverFeatureNewTag('members', 'New', { ...origin, popIn }))
        expect(result.current).toBe('arriving')
        await act(frame)
        const courier = () => [...document.body.children].find(el => el.querySelector('svg'))
        expect(courier()?.textContent).toBe('New')
        act(() => anim.finishAll())
        expect(result.current).toBe('shown')
        await act(frame)
        act(() => anim.finishAll())
        expect(courier()).toBeUndefined()
      } finally {
        anim.restore()
      }
    })
  }

  it('does not bring back a tag that was cleared mid-flight', async () => {
    railWithPill()
    const anim = stubAnimate()
    try {
      const { result } = renderHook(() => useFeatureNewTag('members'))
      act(() => deliverFeatureNewTag('members', 'New', origin))
      await act(frame)
      act(() => clearFeatureNewTag('members'))
      act(() => anim.finishAll())
      expect(result.current).toBeNull()
      await act(frame)
      act(() => anim.finishAll())
      expect(result.current).toBeNull()
    } finally {
      anim.restore()
    }
  })

  it('shows the tag at once when the pill never renders', async () => {
    const row = document.createElement('div')
    row.setAttribute('data-onboarding-nav', 'members')
    visible(row)
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => deliverFeatureNewTag('members', 'New', origin))
    await act(frame)
    expect(result.current).toBe('shown')
  })

  it('ignores storage events for other keys and junk stored state', () => {
    localStorage.setItem('mc-feature-new-tags', '[]')
    const { result } = renderHook(() => useFeatureNewTag('members'))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'something-else' })) })
    expect(result.current).toBeNull()
  })
})

describe('landFeatureGhost timing', () => {
  function stubAnimate() {
    const running: { finish: () => void }[] = []
    const original = Element.prototype.animate
    Element.prototype.animate = function () {
      // Finishes once, as a real animation does: finishing again is a no-op.
      const a = { done: false, onfinish: null as (() => void) | null, finish() { if (!a.done) { a.done = true; a.onfinish?.() } } }
      running.push(a)
      return a as unknown as Animation
    }
    return {
      finishAll: () => [...running].forEach(a => a.finish()),
      restore: () => { Element.prototype.animate = original },
    }
  }
  const frame = () => new Promise(r => requestAnimationFrame(() => r(null)))
  const landedOnce = () => new Promise<void>(resolve => {
    window.addEventListener(FEATURE_LANDED_EVENT, () => resolve(), { once: true })
  })

  it('still fires the landed event when no landing spot appears in time', async () => {
    const real = performance.now.bind(performance)
    let shift = 0
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => real() + shift)
    try {
      const landed = landedOnce()
      landFeatureGhost('members', origin)
      shift = 10_000
      await landed
    } finally {
      spy.mockRestore()
    }
  })

  it('a pointer press skips to the landing and the puff cleans itself up', async () => {
    const anchor = document.createElement('span')
    anchor.setAttribute('data-feature-landing', 'members')
    anchor.setAttribute('data-feature-landing-tile', '#8c9a2b')
    anchor.getClientRects = () => [new DOMRect(0, 0, 36, 36)] as unknown as DOMRectList
    anchor.appendChild(document.createElement('img'))
    document.body.appendChild(anchor)
    const anim = stubAnimate()
    try {
      const landed = landedOnce()
      landFeatureGhost('members', origin)
      await frame()
      window.dispatchEvent(new Event('pointerdown'))
      await landed
      const layers = () => document.body.querySelectorAll('div[aria-hidden="true"]').length
      expect(layers()).toBeGreaterThan(0)
      anim.finishAll()
      expect(layers()).toBe(0)
    } finally {
      anim.restore()
    }
  })
})
