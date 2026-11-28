/**
 * Settings ▸ Chat ▸ Advanced, Context card: the idle compaction switch
 * (`session.idle_compaction`) and its minimum context field
 * (`session.idle_compaction_min_context_pct`). `ChatPanel` renders both rows and
 * calls this once per render.
 *
 * The field follows the server until edited and saves on blur or Enter. The
 * server CLAMPS a minimum outside 40-90 instead of refusing it (a value below the
 * floor is a request for the cheapest setting), so the field sends what was typed,
 * then shows the value saved and says why it differs.
 */
import { useState, useRef, useEffect } from 'react'
import { useMutation, type QueryClient, type UseQueryResult } from '@tanstack/react-query'
import { api } from '../../api/client'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { type ClampNotice, type KirocrewConfigShape, IDLE_COMPACTION_MIN_PCT_DEFAULT } from './chatPanelOptions'
import { setConfigPathValue, type useOptimisticConfigPaths } from './useOptimisticConfigPaths'

const TOGGLE_PATH = 'session.idle_compaction'
const MIN_PATH = 'session.idle_compaction_min_context_pct'
const MIN_MUTATION_KEY = ['patchConfig', MIN_PATH]
const CONFIG_KEY = ['kirocrewConfig']

/** A stored percentage (50 = 50%) in the app language's percent format. */
export const idleCompactionPct = (v: number) => fmtPercent(v / 100, { maximumFractionDigits: 2 })

export function useIdleCompactionFields({
  qc, setPathSaveError, clearOwnPathError, mcQ, overlay, onSaveFailure,
}: {
  qc: QueryClient
  setPathSaveError: (path: string, msg: string) => void
  clearOwnPathError: (path: string) => void
  mcQ: UseQueryResult<KirocrewConfigShape>
  overlay: ReturnType<typeof useOptimisticConfigPaths>
  /** Brings the panel's save-error banner into view: both rows sit below the fold. */
  onSaveFailure: () => void
}) {
  const serverOn = mcQ.data?.session?.idle_compaction ?? false
  // The shared overlay shows the value being saved at click time, and on
  // success writes it into the config cache, so a refetch that fails after a
  // save that worked cannot put the switch back on the old value.
  const idleToggleMut = useMutation({
    ...overlay.mutationOpts<boolean>({
      queryKey: CONFIG_KEY,
      mutationFn: (v: boolean) => api.patchConfig(TOGGLE_PATH, v),
      path: () => TOGGLE_PATH,
      displayValue: v => v,
      applyToCache: (cached, v) => setConfigPathValue(cached as KirocrewConfigShape, TOGGLE_PATH, v),
      onFailure: () => {
        setPathSaveError(TOGGLE_PATH, i18nT('pages.settings.chatPanel.failed_to_save_idle_compaction'))
        onSaveFailure()
      },
      onSupersede: clearOwnPathError,
    }),
    scope: { id: TOGGLE_PATH },
  })
  const idleCompactionOn = overlay.shown(TOGGLE_PATH, serverOn)

  // The config cache is shared and outlives this hook. A minimum save that
  // settles after an unmount (a tab switch and back) must not write the cache,
  // or it could overwrite a newer value that the remounted panel saved. The
  // refetch still runs, so the server's value lands either way.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const [localIdleMinPct, setLocalIdleMinPct] = useState('')
  // The value the clamp notice says was saved. A refetch that brings any other
  // value (another tab, the CLI) makes the notice false, so it is cleared.
  const noticeStoredRef = useRef<number | null>(null)
  // The server's clamp notice is kept as data and turned into text at render
  // time, so a language change re-translates it.
  const [idleMinClamp, setIdleMinClamp] = useState<ClampNotice | null>(null)
  const setIdleMinClampNotice = (notice: ClampNotice | null) => {
    noticeStoredRef.current = notice ? notice.stored : null
    setIdleMinClamp(notice)
  }
  // True only while the user holds a typed, unsaved value: an untouched field
  // follows every refetch, and a blur without an edit saves nothing.
  const idleMinEditedRef = useRef(false)
  const serverMinPct = mcQ.data?.session?.idle_compaction_min_context_pct ?? IDLE_COMPACTION_MIN_PCT_DEFAULT
  useEffect(() => {
    if (!mcQ.isSuccess) return
    if (!idleMinEditedRef.current) setLocalIdleMinPct(String(serverMinPct))
    if (noticeStoredRef.current !== null && noticeStoredRef.current !== serverMinPct) {
      setIdleMinClampNotice(null)
    }
  }, [mcQ.isSuccess, serverMinPct])

  const idleMinPctMut = useMutation({
    // One scope per path: saves run in the order the user made them, also
    // across a remount, so an older value cannot land after a newer one.
    scope: { id: MIN_PATH },
    mutationKey: MIN_MUTATION_KEY,
    mutationFn: (n: number) => api.patchConfig(MIN_PATH, n),
    onMutate: () => clearOwnPathError(MIN_PATH),
    onSuccess: (data: { clamp_notice?: ClampNotice } | undefined, n: number) => {
      const notice = data?.clamp_notice
      // The stored value goes into the cache first, so a failed refetch
      // leaves the field on what the server holds, not on the old value.
      const stored = notice ? notice.stored : n
      const cached = qc.getQueryData<KirocrewConfigShape>(CONFIG_KEY)
      if (mountedRef.current && cached !== undefined) {
        qc.setQueryData(CONFIG_KEY, setConfigPathValue(cached, MIN_PATH, stored))
      }
      if (notice) setLocalIdleMinPct(String(notice.stored))
      setIdleMinClampNotice(notice ?? null)
      return qc.invalidateQueries({ queryKey: CONFIG_KEY })
    },
    // Kept on screen and still marked edited, so the next blur retries it.
    onError: (_err, n) => {
      setPathSaveError(MIN_PATH, i18nT('pages.settings.chatPanel.failed_to_save_idle_compaction_min_context'))
      onSaveFailure()
      idleMinEditedRef.current = true
      setLocalIdleMinPct(String(n))
    },
    // A request can fail after the server stored the value (a 5xx after the
    // write, a dropped response), so a failed save refetches the config, as
    // the switch's overlay does. The draft stays on screen; the cache, and so
    // the next blur's comparison, holds what the server answers.
    onSettled: (_data, err) => {
      if (err) return qc.invalidateQueries({ queryKey: CONFIG_KEY })
    },
  })
  const editIdleMinPct = (v: string) => {
    idleMinEditedRef.current = true
    setLocalIdleMinPct(v)
    setIdleMinClampNotice(null)
  }
  const commitIdleMinPct = () => {
    if (!idleMinEditedRef.current) return
    idleMinEditedRef.current = false
    const n = Number(localIdleMinPct)
    if (localIdleMinPct.trim() === '' || !Number.isFinite(n)) {
      setLocalIdleMinPct(String(serverMinPct))
      return
    }
    // A value equal to the cached one needs no save, unless an earlier save to
    // this path is still pending, also one a previous mount started: then the
    // cache is not yet what the server will hold, and the newer value must be
    // queued behind it so it lands last. The field then matches the server,
    // so a save error left by an earlier attempt no longer applies: a failed
    // save that the server stored anyway reaches here after its refetch.
    if (n === serverMinPct && qc.isMutating({ mutationKey: MIN_MUTATION_KEY }) === 0) {
      clearOwnPathError(MIN_PATH)
      return
    }
    idleMinPctMut.mutate(n)
  }

  // The save worked, so ChatPanel shows this notice in FieldHint's neutral
  // tone, and the text opens with the saved value.
  const idleMinClampNotice = idleMinClamp === null ? '' : i18nT(
    idleMinClamp.requested > idleMinClamp.stored
      ? 'pages.settings.chatPanel.idle_compaction_min_context_lowered'
      : 'pages.settings.chatPanel.idle_compaction_min_context_raised',
    {
      requested: idleCompactionPct(idleMinClamp.requested),
      stored: idleCompactionPct(idleMinClamp.stored),
      min: idleCompactionPct(idleMinClamp.min),
      max: idleCompactionPct(idleMinClamp.max),
    },
  )

  return {
    idleCompactionOn, idleToggleMut, localIdleMinPct, editIdleMinPct, commitIdleMinPct,
    idleMinClampNotice, idleMinPctMut,
  }
}
