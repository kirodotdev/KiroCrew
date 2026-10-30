/**
 * The fold-watch controller: which boards count as shown, and when
 * `fold_watch` is sent (only after a hello, then on each change, and again
 * after the next hello once a reset cleared it). The hook-level path, through
 * a socket, is in `src/test/useWebSocket.foldWatch.test.ts`.
 */
import { describe, it, expect } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { createFoldWatch, watchedBoards } from './foldWatch'

/** Mount an observer on `['command-center', root, 'work']`; returns its unmount. */
function mountBoard(qc: QueryClient, root: string): () => void {
  const observer = new QueryObserver(qc, {
    queryKey: ['command-center', root, 'work'],
    queryFn: () => new Promise<never>(() => {}),
  })
  return observer.subscribe(() => {})
}

describe('fold watch controller', () => {
  it('lists only mounted work-board roots, sorted', () => {
    const qc = new QueryClient()
    const offB = mountBoard(qc, 'b')
    mountBoard(qc, 'a')
    new QueryObserver(qc, { queryKey: ['command-center', 'c', 'questions'], queryFn: () => 1 }).subscribe(() => {})
    qc.getQueryCache().build(qc, { queryKey: ['command-center', 'z', 'work'] }) // no observer
    expect(watchedBoards(qc)).toEqual(['a', 'b'])
    offB()
    expect(watchedBoards(qc)).toEqual(['a'])
  })

  it('sends nothing before the hello, then the set and each change', () => {
    const qc = new QueryClient()
    const sent: unknown[] = []
    const watch = createFoldWatch(qc, (payload) => sent.push(payload))
    const offA = mountBoard(qc, 'a')
    expect(sent).toEqual([])
    watch.onHello()
    expect(sent).toEqual([{ type: 'fold_watch', slots: ['a'] }])
    const offB = mountBoard(qc, 'b')
    expect(sent.at(-1)).toEqual({ type: 'fold_watch', slots: ['a', 'b'] })
    const count = sent.length
    mountBoard(qc, 'b') // a second observer on a board already watched
    expect(sent).toHaveLength(count)
    offA()
    offB()
    expect(sent.at(-1)).toEqual({ type: 'fold_watch', slots: ['b'] })
    watch.dispose()
  })

  it('a reset waits for the next hello, which re-sends the same set', () => {
    const qc = new QueryClient()
    const sent: unknown[] = []
    const watch = createFoldWatch(qc, (payload) => sent.push(payload))
    mountBoard(qc, 'a')
    watch.onHello()
    watch.reset()
    mountBoard(qc, 'b')
    expect(sent).toEqual([{ type: 'fold_watch', slots: ['a'] }])
    watch.onHello()
    expect(sent.at(-1)).toEqual({ type: 'fold_watch', slots: ['a', 'b'] })
    watch.dispose()
  })

  it('stops following the cache once disposed', () => {
    const qc = new QueryClient()
    const sent: unknown[] = []
    const watch = createFoldWatch(qc, (payload) => sent.push(payload))
    watch.onHello()
    watch.dispose()
    mountBoard(qc, 'a')
    expect(sent).toEqual([{ type: 'fold_watch', slots: [] }])
  })
})
