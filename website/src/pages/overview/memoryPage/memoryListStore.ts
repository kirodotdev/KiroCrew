/**
 * One memory list's view and its latest records and counts answer, kept outside
 * React state. Each part of the page reads only its own slice, so a confidence
 * change or a new answer renders the parts that show it, not the whole page.
 */
import { useSyncExternalStore } from 'react'
import type { MemoryRecord, MemoryRecordFacets, MemoryRecordQuery, MemoryStoreDecay } from '../../../types/memoryEditing'
import type { ListView } from './memoryListView'

/** What the page shows from the records and counts reads. */
export type ListAnswer = {
  query: MemoryRecordQuery
  rows: MemoryRecord[]
  total: number
  /** Weaker meaning matches counted but not listed. */
  weaker: number
  /** A new list is on its way and the rows shown belong to another one. */
  searching: boolean
  /** The rows shown are the same list before a narrower answer lands. */
  narrowing: boolean
  recordsError: unknown
  recordsFetching: boolean
  recordsFailed: boolean
  hasNextPage: boolean
  isFetchingNextPage: boolean
  facets?: MemoryRecordFacets
  /** The store's fade rates; only a store whose recall fades experiences sends them. */
  decay?: MemoryStoreDecay
  countsError: unknown
  countsFetching: boolean
  countsFailed: boolean
}

export type ListState = {
  /** The whole view, confidence range included. */
  view: ListView
  /** The view without the confidence range: kept as the same object while only the range moves. */
  base: ListView
  /** Still the view the page opened with. */
  pristine: boolean
  /** The weaker meaning matches are listed too. */
  weak: boolean
  answer: ListAnswer
}

export type ListActions = { refetchRecords: () => void; refetchCounts: () => void; fetchNextPage: () => void }

export type ListStore = {
  get: () => ListState
  subscribe: (listener: () => void) => () => void
  setView: (update: (old: ListView) => ListView) => void
  setWeak: (weak: boolean) => void
  setAnswer: (answer: ListAnswer) => void
  /** Set by the query owner; the list's retry and paging buttons call them. */
  actions: ListActions
  setActions: (actions: ListActions) => void
}

const withoutRange = (view: ListView): ListView => {
  const { conf_min: _min, conf_max: _max, ...filters } = view.filters
  return { ...view, filters }
}

const sameFields = <T extends object>(a: T, b: T) => (Object.keys(a) as (keyof T)[]).every(key => Object.is(a[key], b[key])) && Object.keys(a).length === Object.keys(b).length

export const EMPTY_ANSWER: ListAnswer = {
  query: { q: '', kind: 'all' }, rows: [], total: 0, weaker: 0, searching: true, narrowing: false,
  recordsError: null, recordsFetching: false, recordsFailed: false, hasNextPage: false, isFetchingNextPage: false,
  countsError: null, countsFetching: false, countsFailed: false,
}

export function createListStore(view: ListView): ListStore {
  const listeners = new Set<() => void>()
  let state: ListState = { view, base: withoutRange(view), pristine: true, weak: false, answer: EMPTY_ANSWER }
  const actions: ListActions = { refetchRecords: () => {}, refetchCounts: () => {}, fetchNextPage: () => {} }
  const set = (next: ListState) => { state = next; listeners.forEach(listener => listener()) }
  return {
    get: () => state,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener) } },
    setView: update => {
      const view = update(state.view)
      if (view === state.view) return
      const base = withoutRange(view)
      set({ ...state, view, pristine: false, base: JSON.stringify(base) === JSON.stringify(state.base) ? state.base : base })
    },
    setWeak: weak => { if (weak !== state.weak) set({ ...state, weak }) },
    setAnswer: answer => { if (!sameFields(answer, state.answer)) set({ ...state, answer }) },
    actions,
    setActions: next => { Object.assign(actions, next) },
  }
}

/** One slice of the list's state; the caller renders again only when that slice changes.
 *  The selector must return a value the store already holds, not a new object. */
export function useListStore<T>(store: ListStore, select: (state: ListState) => T): T {
  return useSyncExternalStore(store.subscribe, () => select(store.get()))
}
