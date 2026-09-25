import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { BackendRow, BackendInvalidRow, BackendUnroutableRow } from '../api/client'

/** What every backend picker reads: the selectable rows, the id an unselected
 *  new chat lands on, and the two operator-descriptor diagnostic lists. */
export interface BackendListingState {
  /** Selectable rows only. Every one of these is currently startable — unlike
   *  the salvaged harness listing, the descriptor-harness `/api/backends` gates
   *  selectability server-side (the single `resolve_selected_backend` owner), so
   *  a row that reaches this array is one the picker may offer without a
   *  post-click refusal. */
  backends: BackendRow[]
  /** Operator descriptors that failed validation — never spellable, so never a
   *  session backend. Served apart so a surface that lists selectable rows cannot
   *  accidentally offer one. */
  invalid: BackendInvalidRow[]
  /** Operator descriptors that are spellable but declared no verified routing, so
   *  they are known-but-unselectable (D3). Also never offered as a pick. */
  unroutable: BackendUnroutableRow[]
  /** The backend an unselected new chat is created on. Preselect THIS, so the
   *  highlighted row cannot disagree with what an unselected creation does.
   *  Derived from the `is_global_default` row rather than a separate field, so it
   *  is always one of the selectable ids. */
  defaultId: string
  /** The listing fetch did not answer. All arrays are empty in this state. */
  isError: boolean
  isLoading: boolean
}

const EMPTY_BACKENDS: BackendRow[] = []
const EMPTY_INVALID: BackendInvalidRow[] = []
const EMPTY_UNROUTABLE: BackendUnroutableRow[] = []

/**
 * THE backend listing. Every selection surface reads it through here.
 *
 * ## Why one hook and not a `useQuery` per surface
 *
 * The same reason `useAvailableModels` exists: React Query stores one cache
 * entry per key and the fetching observer's options win, so two surfaces sharing
 * the `['backends']` key while declaring their own `queryFn` let *whichever
 * fetched last* decide the shape everyone reads.
 *
 * ## Why `isError` empties the list instead of keeping the last one
 *
 * React Query retains `data` across a failed refetch. For this resource that is
 * the wrong call: whether a backend is selectable is a statement about the build
 * and its governance ceiling *right now*, and an operator descriptor can drop out
 * of the listing between fetches. An empty list is an honest "we do not know
 * yet"; a retained one is a confident wrong answer that could offer a pick the
 * gateway now refuses.
 */
export function useBackends(): BackendListingState {
  const { data, isError, isLoading } = useQuery({
    queryKey: ['backends'],
    queryFn: async () => {
      const r = await api.backends()
      const backends = Array.isArray(r?.backends) ? r.backends : []
      return {
        backends,
        invalid: Array.isArray(r?.invalid) ? r.invalid : [],
        unroutable: Array.isArray(r?.unroutable) ? r.unroutable : [],
        // Derived, never a second field: the default is whichever selectable row
        // the gateway flagged, so it can never name an id the picker would then
        // mark unpickable. `''` (kiro-cli) is a real id, so the flag — not
        // falsiness of the id — is what identifies the default row.
        defaultId: backends.find(b => b.is_global_default)?.id ?? '',
      }
    },
  })
  if (isError || !data) {
    return {
      backends: EMPTY_BACKENDS,
      invalid: EMPTY_INVALID,
      unroutable: EMPTY_UNROUTABLE,
      defaultId: '',
      isError,
      isLoading,
    }
  }
  return { ...data, isError, isLoading }
}

/** The row for `id`, or undefined. `id` null/undefined means "inherit the
 *  default", which resolves to the default row — that is what a surface must
 *  display, because it is what an unselected creation will really run on.
 *
 *  `''` is a REAL backend id (kiro-cli), never "no pin": a chat pinned to `''`
 *  resolves to the kiro-cli row even when the default is another harness. */
export function backendRow(
  state: BackendListingState,
  id: string | null | undefined,
): BackendRow | undefined {
  const wanted = typeof id === 'string' ? id : state.defaultId
  // `wanted` can legitimately be `''` (kiro-cli); match on the id directly rather
  // than on truthiness so the kiro-cli row is found.
  return state.backends.find(b => b.id === wanted)
}

/**
 * What the composer's backend chip should call the chat's backend.
 *
 * `pinned` is the slot's stored `acp_backend` (`null`/`undefined` = inherits the
 * default; `''` = pinned to kiro-cli). A row the listing names wins. A PINNED id
 * the listing cannot name (loading, errored, or dropped from the listing) shows
 * as its raw id: calling it "default" would be a wrong claim about which backend
 * serves a pinned chat. Only an unpinned chat with no listing falls to
 * `defaultLabel`.
 */
export function backendChipLabel(
  state: BackendListingState,
  pinned: string | null | undefined,
  defaultLabel: string,
): string {
  const row = backendRow(state, pinned)
  if (row) return row.label || row.id
  return typeof pinned === 'string' && pinned ? pinned : defaultLabel
}

/**
 * Whether there is a backend DECISION to surface at all. A single-backend
 * install (the common case) has none: the welcome picker would offer one row and
 * the shelf chip would name the only thing it could — chrome with nothing behind
 * it, seen every session. A pinned chat (`''` included) is a decision regardless
 * of what the listing offers now, so its chip always shows.
 */
export function backendChoiceExists(state: BackendListingState, pinned: string | null | undefined): boolean {
  return state.backends.length > 1 || typeof pinned === 'string'
}
