import { describe, expect, it, vi } from 'vitest'
import { createListStore, EMPTY_ANSWER } from '../pages/overview/memoryPage/memoryListStore'
import { DEFAULT_VIEW } from '../pages/overview/memoryPage/memoryListView'

describe('memory list store', () => {
  it('keeps the view without the confidence range while only the range moves', () => {
    const list = createListStore({ ...DEFAULT_VIEW, filters: { tag: ['a'] } })
    const base = list.get().base
    expect(base.filters).toEqual({ tag: ['a'] })
    list.setView(old => ({ ...old, filters: { ...old.filters, conf_min: 0.4 } }))
    expect(list.get().view.filters).toEqual({ tag: ['a'], conf_min: 0.4 })
    expect(list.get().base).toBe(base)
    expect(list.get().pristine).toBe(false)
    list.setView(old => ({ ...old, filters: { ...old.filters, tag: ['b'] } }))
    expect(list.get().base).not.toBe(base)
    expect(list.get().base.filters).toEqual({ tag: ['b'] })
  })

  it('tells subscribers only about an answer that changed', () => {
    const list = createListStore(DEFAULT_VIEW)
    const listener = vi.fn()
    list.subscribe(listener)
    list.setAnswer({ ...EMPTY_ANSWER })
    expect(listener).not.toHaveBeenCalled()
    list.setAnswer({ ...EMPTY_ANSWER, total: 3 })
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
