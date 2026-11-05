/**
 * A side panel slide holds per-frame width work (utils/panelSlide): while any
 * slide runs, deferred work waits; when the last one ends it runs once each.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { afterSlide, beginPanelSlide, forgetAfterSlide, slideHeld } from './panelSlide'

const root = () => document.documentElement

describe('panelSlide', () => {
  afterEach(() => { expect(slideHeld()).toBe(false) })

  it('holds while a slide runs, marks the root, and runs deferred work once at the end', () => {
    const work = vi.fn()
    expect(slideHeld()).toBe(false)
    const end = beginPanelSlide()
    expect(slideHeld()).toBe(true)
    expect(root().hasAttribute('data-panel-sliding')).toBe(true)
    afterSlide(work)
    afterSlide(work)
    expect(work).not.toHaveBeenCalled()
    end()
    expect(work).toHaveBeenCalledTimes(1)
    expect(root().hasAttribute('data-panel-sliding')).toBe(false)
    // Ending twice is safe and runs nothing again.
    end()
    expect(work).toHaveBeenCalledTimes(1)
  })

  it('waits for the last of two slides (a settle grabbed by a new drag)', () => {
    const work = vi.fn()
    const a = beginPanelSlide()
    const b = beginPanelSlide()
    afterSlide(work)
    a()
    expect(slideHeld()).toBe(true)
    expect(work).not.toHaveBeenCalled()
    b()
    expect(work).toHaveBeenCalledTimes(1)
  })

  it('forgets work whose owner has gone', () => {
    const work = vi.fn()
    const end = beginPanelSlide()
    afterSlide(work)
    forgetAfterSlide(work)
    end()
    expect(work).not.toHaveBeenCalled()
  })
})
