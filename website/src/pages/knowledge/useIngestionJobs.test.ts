/**
 * useIngestionJobs — the Sources tab's upload progress rows.
 *
 * The defect: when upload A settled it started a 5 s timer that cleared the
 * rows, and nothing ever cancelled it. Upload B started inside that window had
 * its own rows wiped by A's timer. The label check pins that the row text is
 * catalog copy, not the raw status string.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useIngestionJobs, INGESTION_ROWS_LINGER_MS } from './useIngestionJobs'
import { ingestionStatusLabel } from './knowledgeUtils'
import { i18nT } from '../../i18n/t'

describe('useIngestionJobs', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it("keeps a second upload's rows when the first upload's clear timer would fire", () => {
    const { result } = renderHook(() => useIngestionJobs())

    let a = 0
    act(() => { a = result.current.start([{ name: 'a.md', status: 'uploading' }]) })
    act(() => { result.current.update(a, [{ name: 'a.md', status: 'done' }]) })
    act(() => { result.current.scheduleClear(a) })

    act(() => { vi.advanceTimersByTime(1000) })
    act(() => { result.current.start([{ name: 'b.md', status: 'uploading' }]) })
    act(() => { vi.advanceTimersByTime(INGESTION_ROWS_LINGER_MS) })

    expect(result.current.jobs).toEqual([{ name: 'b.md', status: 'uploading' }])
  })

  it('ignores updates and clears from an upload a newer one replaced', () => {
    const { result } = renderHook(() => useIngestionJobs())

    let a = 0
    act(() => { a = result.current.start([{ name: 'a.md', status: 'uploading' }]) })
    act(() => { result.current.start([{ name: 'b.md', status: 'uploading' }]) })
    act(() => { result.current.update(a, [{ name: 'a.md', status: 'done' }]) })
    act(() => { result.current.scheduleClear(a) })
    act(() => { vi.advanceTimersByTime(INGESTION_ROWS_LINGER_MS) })

    expect(result.current.jobs).toEqual([{ name: 'b.md', status: 'uploading' }])
  })

  it('clears the rows once the newest upload settles and the window passes', () => {
    const { result } = renderHook(() => useIngestionJobs())

    let b = 0
    act(() => { b = result.current.start([{ name: 'b.md', status: 'uploading' }]) })
    act(() => { result.current.update(b, [{ name: 'b.md', status: 'done' }]) })
    act(() => { result.current.scheduleClear(b) })
    act(() => { vi.advanceTimersByTime(INGESTION_ROWS_LINGER_MS - 1) })
    expect(result.current.jobs).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(1) })

    expect(result.current.jobs).toEqual([])
  })

  it('cancels the pending clear on unmount', () => {
    const { result, unmount } = renderHook(() => useIngestionJobs())

    let a = 0
    act(() => { a = result.current.start([{ name: 'a.md', status: 'done' }]) })
    act(() => { result.current.scheduleClear(a) })
    unmount()

    expect(vi.getTimerCount()).toBe(0)
  })

  it('arms no timer when an upload settles after the page unmounted', () => {
    const { result, unmount } = renderHook(() => useIngestionJobs())

    let a = 0
    act(() => { a = result.current.start([{ name: 'a.md', status: 'uploading' }]) })
    const { scheduleClear } = result.current
    unmount()
    scheduleClear(a)

    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('ingestionStatusLabel', () => {
  it('shows catalog copy for the uploading and done statuses', () => {
    expect(ingestionStatusLabel('uploading'))
      .toBe(i18nT('pages.knowledge.sourcesList.ingest_status_uploading'))
    expect(ingestionStatusLabel('done'))
      .toBe(i18nT('pages.knowledge.sourcesList.ingest_status_done'))
  })
})
