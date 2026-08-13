import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSortableTable } from '../hooks/useSortableTable'

interface Item { name: string; value: number }
const data: Item[] = [
  { name: 'Charlie', value: 3 },
  { name: 'Alice', value: 1 },
  { name: 'Bob', value: 2 },
]
const comparators = {
  name: (a: Item, b: Item) => a.name.localeCompare(b.name),
  value: (a: Item, b: Item) => a.value - b.value,
}

describe('useSortableTable', () => {
  beforeEach(() => localStorage.clear())

  it('returns unsorted data by default', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test', comparators))
    expect(result.current.sorted.map(d => d.name)).toEqual(['Charlie', 'Alice', 'Bob'])
    expect(result.current.sort).toEqual({ key: null, dir: null })
  })

  it('applies default sort', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-def', comparators, { key: 'name', dir: 'asc' }))
    expect(result.current.sorted.map(d => d.name)).toEqual(['Alice', 'Bob', 'Charlie'])
  })

  it('cycles asc -> desc -> default on toggle', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-cycle', comparators))
    act(() => result.current.toggle('name'))
    expect(result.current.sort).toEqual({ key: 'name', dir: 'asc' })
    expect(result.current.sorted.map(d => d.name)).toEqual(['Alice', 'Bob', 'Charlie'])

    act(() => result.current.toggle('name'))
    expect(result.current.sort).toEqual({ key: 'name', dir: 'desc' })
    expect(result.current.sorted.map(d => d.name)).toEqual(['Charlie', 'Bob', 'Alice'])

    act(() => result.current.toggle('name'))
    expect(result.current.sort).toEqual({ key: null, dir: null })
  })

  it('switches to asc when clicking a different column', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-switch', comparators))
    act(() => result.current.toggle('name'))
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'asc' })
    expect(result.current.sorted.map(d => d.value)).toEqual([1, 2, 3])
  })

  it('persists sort to localStorage', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-persist', comparators))
    act(() => result.current.toggle('name'))
    expect(JSON.parse(localStorage.getItem('sort:test-persist')!)).toEqual({ key: 'name', dir: 'asc' })
  })

  it('restores sort from localStorage', () => {
    localStorage.setItem('sort:test-restore', JSON.stringify({ key: 'value', dir: 'desc' }))
    const { result } = renderHook(() => useSortableTable(data, 'test-restore', comparators))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'desc' })
    expect(result.current.sorted.map(d => d.value)).toEqual([3, 2, 1])
  })

  it('returns to defaultSort on third click', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-def-cycle', comparators, { key: 'value', dir: 'asc' }))
    act(() => result.current.toggle('name'))
    act(() => result.current.toggle('name'))
    act(() => result.current.toggle('name'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'asc' })
  })

  it('bidirectional option flips asc <-> desc without ever resetting', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-bidir', comparators, { key: 'name', dir: 'asc' }, { bidirectional: true }))
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'asc' })
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'desc' })
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'asc' })
  })

  it('initialDirs sets the first-selection direction per column', () => {
    const { result } = renderHook(() => useSortableTable(data, 'test-initdir', comparators, { key: 'name', dir: 'asc' }, { initialDirs: { value: 'desc' }, bidirectional: true }))
    // First selection of value opens descending per initialDirs.
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'desc' })
    // Then it flips bidirectionally.
    act(() => result.current.toggle('value'))
    expect(result.current.sort).toEqual({ key: 'value', dir: 'asc' })
  })
})
