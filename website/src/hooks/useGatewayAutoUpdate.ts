/**
 * The gateway's `auto_update` switch as Settings → About and the post-update
 * "What's new" modal read and write it. (Developer → Config's raw config editor
 * keeps its own plain toggle.)
 *
 * - `checked`: a save in flight, else the saved value from the shared
 *   `['kirocrewConfig']` entry (which a refresh frame re-reads), else the
 *   value the latest successful save reported (kept across a later failed
 *   save), else the schema default, on. `held` only while the first read is
 *   in flight; after a failed read the switch stays usable.
 * - `set`: an optimistic write through `useOptimisticConfigPaths`. Saves run
 *   one at a time in click order, since the gateway keeps whichever write
 *   arrives last. Only the latest of overlapping saves settles the cache; it
 *   writes the value the response accepted, then the config is re-read either
 *   way, since a write can persist even when its response is lost.
 * - `overlayPinned`: config.local.json sets the key, as the latest answer that
 *   reports it said (a save, a refusal, a check). The switch stays usable, so
 *   a click after the overlay is removed is what clears the note.
 * - `readFailed` / `refreshFailed` / `saveError`: what the row reports, each
 *   describing the position `checked` shows (no read notice while a click is
 *   in flight, since the switch shows the click). A save
 *   failure is cleared for good once a read made after it shows the value it
 *   asked for: that write landed, and only its answer was lost.
 */
import { useEffect, useState } from 'react'
import { skipToken, useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { failedWithNoData } from '../api/queryState'
import { setConfigPathValue, useOptimisticConfigPaths } from '../pages/settings/useOptimisticConfigPaths'
import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import { parseErrorCode } from '../utils/errorReport'

const AUTO_UPDATE_PATH = 'auto_update'
const CONFIG_KEY = ['kirocrewConfig'] as const
/** Whether config.local.json sets `auto_update`: client-held, never fetched. */
const AUTO_UPDATE_OVERLAY_KEY = ['update.auto_update.overlay'] as const
/**
 * The value the latest successful save reported the gateway holds:
 * client-held, never fetched. Read only while the config entry has no data,
 * and shared so About and the What's-new modal agree.
 */
const AUTO_UPDATE_ANSWER_KEY = ['update.auto_update.answered'] as const

type KirocrewConfigAutoUpdate = { auto_update?: unknown }
/** A server answer that may carry the effective value and the overlay pin. */
type AutoUpdateAnswer = { auto_update?: unknown; overlay_override?: unknown }

export type SwitchState = {
  /** The position the switch shows. */
  checked: boolean
  /** The first read has not answered, so the switch is held at its default. */
  held: boolean
  /** No read has answered and no value is otherwise known: `checked` is the default. */
  readFailed: boolean
  /**
   * The notice to show when `checked` is a known value the latest read could
   * not confirm (the last value read, or the value a save answered with).
   */
  refreshFailed: string | null
  set: (next: boolean) => void
  saveError: string | null
}

/** The switch's 409 refusal: config.local.json owns the key. */
function isOverlayRefusal(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && parseErrorCode(err.body) === 'auto_update_overlay_owned'
}

function storeOverlayPin(qc: QueryClient, answer: AutoUpdateAnswer | undefined): void {
  if (typeof answer?.overlay_override === 'boolean') qc.setQueryData(AUTO_UPDATE_OVERLAY_KEY, answer.overlay_override)
}

/**
 * How many times the config entry's data has been set (a read or a write), so
 * a later answer can tell whether the entry moved after it was asked for.
 */
export function configReads(qc: QueryClient): number {
  return qc.getQueryState(CONFIG_KEY)?.dataUpdateCount ?? 0
}

/**
 * Take the saved value and the overlay pin from a check's answer. The value
 * only when the config entry has not moved since the check was sent
 * (`readsWhenSent`, from `configReads`): otherwise the check may have read it
 * before a save that has landed since. A save still in flight settles after
 * this and writes over it either way.
 */
export function adoptCheckedAutoUpdate(qc: QueryClient, answer: AutoUpdateAnswer | undefined, readsWhenSent: number | undefined): void {
  storeOverlayPin(qc, answer)
  const value = answer?.auto_update
  if (typeof value !== 'boolean') return
  if (configReads(qc) !== readsWhenSent) return
  qc.setQueryData(CONFIG_KEY, (old: unknown) => (old === undefined ? old : setConfigPathValue(old, AUTO_UPDATE_PATH, value)))
}

export function useGatewayAutoUpdate(
  /** `true` re-reads the saved value on every mount (the What's-new modal); otherwise a value over 30 s old. */
  { readOnMount = false }: { readOnMount?: boolean } = {},
): SwitchState & { overlayPinned: boolean } {
  const qc = useQueryClient()
  const q = useQuery<KirocrewConfigAutoUpdate, Error, boolean>({
    queryKey: CONFIG_KEY,
    queryFn: () => api.kirocrewConfig(),
    // Absent means the schema default, which is on.
    select: c => (c?.auto_update === undefined ? true : c.auto_update === true),
    ...(readOnMount ? { refetchOnMount: 'always' as const } : { staleTime: 30_000, refetchOnWindowFocus: false }),
  })
  const pin = useQuery<boolean>({ queryKey: AUTO_UPDATE_OVERLAY_KEY, queryFn: skipToken, staleTime: Infinity, gcTime: Infinity })
  const answer = useQuery<boolean>({ queryKey: AUTO_UPDATE_ANSWER_KEY, queryFn: skipToken, staleTime: Infinity, gcTime: Infinity })

  const { shown, mutationOpts } = useOptimisticConfigPaths(qc)
  // `reads`: how many reads the config entry had taken when the save failed.
  const [failure, setFailure] = useState<{ err: unknown; requested: boolean; reads: number } | null>(null)
  const opts = mutationOpts<boolean, AutoUpdateAnswer>({
    queryKey: CONFIG_KEY,
    mutationFn: next => api.setAutoUpdate(next),
    path: () => AUTO_UPDATE_PATH,
    displayValue: next => next,
    applyToCache: (cached, next, data) =>
      setConfigPathValue(cached, AUTO_UPDATE_PATH, typeof data?.auto_update === 'boolean' ? data.auto_update : next),
    onFailure: (err, requested) => setFailure({ err, requested, reads: configReads(qc) }),
    onSupersede: () => setFailure(null),
  })
  const save = useMutation({
    ...opts,
    scope: { id: 'update.auto_update' },
    onSuccess: (data: AutoUpdateAnswer, next: boolean, token: number) => {
      storeOverlayPin(qc, data)
      // Saves run in click order, so the last success is what the gateway
      // holds; a later failed save leaves it standing.
      qc.setQueryData(AUTO_UPDATE_ANSWER_KEY, typeof data?.auto_update === 'boolean' ? data.auto_update : next)
      return opts.onSuccess(data, next, token)
    },
    onError: (err: unknown, next: boolean, token: number | undefined) => {
      if (isOverlayRefusal(err)) qc.setQueryData(AUTO_UPDATE_OVERLAY_KEY, true)
      opts.onError(err, next, token)
    },
  })
  // A read made after the failure that shows the requested value means the
  // write landed. One from before it (still cached at the time) proves nothing.
  useEffect(() => {
    if (failure && configReads(qc) > failure.reads && q.data === failure.requested) setFailure(null)
  }, [failure, q.data, q.dataUpdatedAt, qc])

  const neverRead = failedWithNoData(q)
  // With nothing read, the last successful save's answer is the best known value.
  const saved = q.data ?? answer.data
  // A save in flight: the switch shows the click, so no read notice applies.
  const clicked = shown<boolean | undefined>(AUTO_UPDATE_PATH, undefined)
  const refused = !!failure && isOverlayRefusal(failure.err)
  return {
    checked: clicked ?? saved ?? true,
    held: saved === undefined && !neverRead,
    readFailed: neverRead && saved === undefined && clicked === undefined,
    refreshFailed: clicked !== undefined
      ? null
      : q.isError && q.data !== undefined
        ? i18nT('pages.settings.aboutPanel.auto_update_refresh_failed')
        : neverRead && saved !== undefined
          ? i18nT('pages.settings.aboutPanel.auto_update_save_answer_shown')
          : null,
    overlayPinned: pin.data === true,
    set: next => save.mutate(next),
    saveError: !failure
      ? null
      : refused
        ? i18nT('pages.settings.privacyPanel.recordMetricsOverlayPinned')
        : errMessage(failure.err) || i18nT('pages.settings.aboutPanel.auto_update_save_failed'),
  }
}
