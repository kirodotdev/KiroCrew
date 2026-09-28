import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import { readFolderSortMode, type FolderSortMode } from '../utils/folderTree'

export interface FolderSortModeRead {
  /**
   * The mode to draw with: the person's once a config body is on hand (fresh
   * or cached), `custom` (the stored order every earlier build drew) before
   * one arrives, after a read failed with none to fall back on, or from an
   * older gateway without the field.
   */
  readonly mode: FolderSortMode
  /**
   * A config body has been read -- `mode` is the person's (or the last one
   * read), not the fallback. The reorder affordance requires it: a sibling
   * drag writes stored positions computed from the DRAWN order, sound only
   * when that order is known to be the stored one.
   */
  readonly known: boolean
  /**
   * The read FAILED WITH NOTHING TO SHOW: no body has ever arrived, and a fetch
   * has errored. The raw server string (the generic fallback when the failure
   * carried none, or for an observer mounted mid-retry that never saw it), so
   * an `ErrorNotice` given it as `message` recovers the structured report from
   * the error journal by message match. `null` while a body is on hand --
   * including a body a failed BACKGROUND refetch kept, which is drawn and
   * acted on, and which react-query retries on its own, so nothing about it is
   * said. HELD through every retry's pending phase (status `pending`, still no
   * body) and cleared only when a body arrives, so a banner keyed on it does
   * not unmount and remount around automatic retries.
   */
  readonly error: string | null
}

/** The slice of the `['kirocrewConfig']` body this read looks at. */
export type FolderSortConfigBody = { dashboard?: { folder_sort?: unknown } }

/**
 * One `['kirocrewConfig']` query observation, read as a folder sort mode --
 * the SINGLE derivation every surface keys on. Shared by
 * {@link useFolderSortMode} (which FETCHES the entry), the sidebar (its own
 * observer of the same key, with the optimistic overlay layered on the mode)
 * and the Command Bar (a subscriber that never fetches), so the three cannot
 * read the same body three ways.
 *
 * Keyed on what is KNOWN, not on the transient query status: `status` flips
 * `success` -> `error` -> `pending` -> `success` across a failed background
 * refetch and its retry while the body on hand never changes, and a UI keyed
 * on it says "unreadable" beside a list it is confidently drawing, then drops
 * that banner for the length of each retry. Here a body on hand is known and
 * silent whatever the last fetch did, and a failure with no body is said once
 * and held until a body arrives.
 */
export function useFolderSortRead(q: {
  data: FolderSortConfigBody | undefined
  status: 'pending' | 'error' | 'success'
  error: unknown
  /**
   * When the query last errored (0 = never). react-query keeps it across the
   * re-pending a data-less refetch causes, so it is what tells a FRESH observer
   * -- one mounted after the failure, whose own mount started the retry -- that
   * a failure is on record while `status` reads `pending` and `error` is null.
   * The shell's credit pill reads the same signal for the same reason.
   */
  errorUpdatedAt: number
}): FolderSortModeRead {
  const known = q.data !== undefined
  // Failed on record: no body has ever arrived, and a fetch has errored. Holds
  // through every retry's pending phase, for every observer, latch or no latch.
  const failedOnRecord = !known && q.errorUpdatedAt > 0
  const liveFailure = q.status === 'error' && !known
    ? (errMessage(q.error) || i18nT('components.errorBoundary.something_went_wrong'))
    : null
  // The latch carries the server's own words across the retry's pending phase
  // for THIS instance (React's "storing information from previous renders"
  // shape: the value returned below is right on this very render). A fresh
  // instance has none and says the generic words until the retry settles.
  const [latched, setLatched] = useState<string | null>(null)
  if (liveFailure !== null && latched !== liveFailure) setLatched(liveFailure)
  else if (known && latched !== null) setLatched(null)
  return {
    mode: readFolderSortMode(q.data?.dashboard?.folder_sort),
    known,
    error: failedOnRecord
      ? (liveFailure ?? latched ?? i18nT('components.errorBoundary.something_went_wrong'))
      : null,
  }
}

/**
 * The person's sidebar folder sort mode (`dashboard.folder_sort`), for every
 * surface that draws the folder tree outside the sidebar itself — the move-to
 * submenu, the new-chat-in-folder suggestion, the cron job form's folder picker.
 * (The Command Bar's Search Folders view reads the same entry through
 * {@link useFolderSortRead} without fetching, for the reason given there.)
 *
 * Read through the shared `['kirocrewConfig']` query rather than a dedicated
 * fetch: the sidebar already holds that query for its own settings, the sidebar
 * menu writes the mode through `api.patchConfig` and settles it back into the
 * same cache entry, so a picker opened right after a switch draws the new order
 * without a request of its own — and cannot lag behind the sidebar it sits next
 * to. The value is normalized by `readFolderSortMode`, so an older gateway
 * without the field, or a value this build does not know, reads as `custom`:
 * the stored order every earlier build drew.
 *
 * The read's failure travels with the mode (`error`) rather than being dropped
 * here, for the hosts that draw the tree on a screen with no sidebar to say it
 * (the job form on the Schedule page, the Command Bar): a picker silently drawn
 * in the stored order behind a mode the person did choose is a dead end. A host
 * that sits beside the sidebar's own banner leaves it unsaid -- one notice per
 * screen.
 */
export function useFolderSortMode(): FolderSortModeRead {
  const q = useQuery<FolderSortConfigBody>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  return useFolderSortRead(q)
}
