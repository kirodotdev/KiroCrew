import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSentCommentIds } from './useSentCommentIds'

// The sent-id store both batch Submit surfaces (the chat side panel and the
// standalone artifact page) read their pending count from.
describe('useSentCommentIds', () => {
  beforeEach(() => { localStorage.clear() })

  it('reads the ids an earlier mount persisted for this artifact', () => {
    localStorage.setItem('mc-cmt-sent:doc', JSON.stringify(['c1', 'c2']))
    const { result } = renderHook(() => useSentCommentIds('doc'))
    expect([...result.current.sentIds].sort()).toEqual(['c1', 'c2'])
  })

  it('reads a corrupt or absent entry as nothing sent yet', () => {
    localStorage.setItem('mc-cmt-sent:bad', '{not json')
    expect(renderHook(() => useSentCommentIds('bad')).result.current.sentIds.size).toBe(0)
    expect(renderHook(() => useSentCommentIds('none')).result.current.sentIds.size).toBe(0)
  })

  it('marks a batch sent, keeps earlier ids, and persists the union', () => {
    localStorage.setItem('mc-cmt-sent:doc', JSON.stringify(['c1']))
    const { result } = renderHook(() => useSentCommentIds('doc'))
    act(() => { result.current.markSent([{ id: 'c2' }, { id: 'c3' }]) })
    expect([...result.current.sentIds].sort()).toEqual(['c1', 'c2', 'c3'])
    expect(JSON.parse(localStorage.getItem('mc-cmt-sent:doc') ?? '[]').sort()).toEqual(['c1', 'c2', 'c3'])
  })

  it('switches to the new artifact\'s ids and writes under its own key', () => {
    localStorage.setItem('mc-cmt-sent:a', JSON.stringify(['a1']))
    localStorage.setItem('mc-cmt-sent:b', JSON.stringify(['b1']))
    const { result, rerender } = renderHook(({ slug }) => useSentCommentIds(slug), { initialProps: { slug: 'a' } })
    expect([...result.current.sentIds]).toEqual(['a1'])
    rerender({ slug: 'b' })
    expect([...result.current.sentIds]).toEqual(['b1'])
    act(() => { result.current.markSent([{ id: 'b2' }]) })
    expect(JSON.parse(localStorage.getItem('mc-cmt-sent:b') ?? '[]').sort()).toEqual(['b1', 'b2'])
    expect(JSON.parse(localStorage.getItem('mc-cmt-sent:a') ?? '[]')).toEqual(['a1'])
  })
})
