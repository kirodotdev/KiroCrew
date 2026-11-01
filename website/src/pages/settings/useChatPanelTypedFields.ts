/**
 * Settings ▸ Chat's typed number and path fields: completion-event characters, the
 * turn time limit, the new-session folder, the auto-compact threshold's exact value
 * and the compaction wait budget. Each holds a
 * local draft that follows the server until edited and saves on blur or Enter; the
 * completion-keep select's save sits with them in the Subagents card. `ChatPanel`
 * calls it once per render, between its two Model-page hooks.
 */
import { useState, useRef, useEffect, useId } from 'react'
import { useMutation, type QueryClient, type UseQueryResult } from '@tanstack/react-query'
import type { DashboardConfig } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
import { useSettingsDeepLinkTarget } from '../../components/settings'
import { settingAnchorSelector } from '../../hooks/useSettingHighlight'
import { apiErrorCode } from '../../api/apiError'
import { reportForError, type ErrorReport } from '../../utils/errorReport'
import {
  TURN_LIMIT_DEFAULT, type CompletionKeepMode, COMPLETION_KEEP_CHARS_DEFAULT, type ClampNotice,
  type KirocrewConfigShape, COMPACT_CUSTOM, compactSelectValue, clampAutocompactPct,
  AUTOCOMPACT_PCT_MIN, AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_DEFAULT,
} from './chatPanelOptions'

const AUTOCOMPACT_PATH = 'session.autocompact_pct'

export function useChatPanelTypedFields({
  qc, setSaveError, setPathSaveError, clearOwnPathError, dashQ, mcQ, mcCfg,
}: {
  qc: QueryClient
  setSaveError: (msg: string) => void
  /** The panel's path-scoped banner writers: a save may clear only its own path's failure. */
  setPathSaveError: (path: string, msg: string) => void
  clearOwnPathError: (path: string) => void
  dashQ: UseQueryResult<DashboardConfig>
  mcQ: UseQueryResult<KirocrewConfigShape>
  mcCfg: KirocrewConfigShape | undefined
}) {
  const [localKeepChars, setLocalKeepChars] = useState('')
  // Seeded ONCE, in the render that first has the served config: setting state
  // during render re-renders before anything commits, so the commit that enables
  // the input is the one that shows the stored number. An effect would commit one
  // frame with the input enabled and empty, which a user can see and a test can
  // land on. One-shot (unlike the follow-the-server fields below) because this
  // row's draft is its own: a refetch must not overwrite what is being typed.
  const [keepCharsSeeded, setKeepCharsSeeded] = useState(false)
  if (mcQ.data && !keepCharsSeeded) {
    setKeepCharsSeeded(true)
    setLocalKeepChars(String(mcQ.data.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT))
  }

  const keepCharsMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.completion_keep_chars', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_characters'))
      setLocalKeepChars(
        String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
      )
    },
  })

  // ── Turn time limit (agent.chat_turn_timeout_secs) ──
  // Same follow-the-server rule as the compaction budget below: an untouched
  // field tracks every refetch, and a blur without an edit saves nothing.
  const [localTurnLimit, setLocalTurnLimit] = useState('')
  const turnLimitEditedRef = useRef(false)
  const serverTurnLimit = mcQ.data?.agent?.chat_turn_timeout_secs
  useEffect(() => {
    if (mcQ.isSuccess && !turnLimitEditedRef.current) {
      setLocalTurnLimit(String(serverTurnLimit ?? TURN_LIMIT_DEFAULT))
    }
  }, [mcQ.isSuccess, serverTurnLimit])

  const turnLimitMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.chat_turn_timeout_secs', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_turn_time_limit'))
      setLocalTurnLimit(String(mcCfg?.agent?.chat_turn_timeout_secs ?? TURN_LIMIT_DEFAULT))
    },
  })

  // ── New session folders (dashboard.session_folder_root) ──
  // Same follow-the-server rule as the turn limit: an untouched field tracks
  // every refetch, and a blur without an edit saves nothing. A refused path
  // keeps the typed text on screen with the reason under the field, so it can
  // be corrected rather than retyped; the server names the resolved path it saved.
  const [localFolderRoot, setLocalFolderRoot] = useState('')
  const [folderRootError, setFolderRootError] = useState<{ message: string; report?: ErrorReport } | null>(null)
  // Set by a save that landed, cleared by the next edit: the field saves on its
  // own (blur or Enter) with no button, so this is the only sign it took.
  const [folderRootSaved, setFolderRootSaved] = useState(false)
  const folderRootErrorId = useId()
  const folderRootEditedRef = useRef(false)
  const folderRootIme = useImeGuard()
  const serverFolderRoot = dashQ.data?.session_folder_root ?? ''
  useEffect(() => {
    if (dashQ.isSuccess && !folderRootEditedRef.current) setLocalFolderRoot(serverFolderRoot)
  }, [dashQ.isSuccess, serverFolderRoot])
  const folderRootMut = useMutation({
    mutationFn: (root: string) => api.updateDashboardConfig({ session_folder_root: root }),
    onSuccess: () => {
      folderRootEditedRef.current = false
      setFolderRootError(null)
      setFolderRootSaved(true)
      return qc.invalidateQueries({ queryKey: ['dashboardConfig'] })
    },
    // Only the server's own refusal of the path gets the "pick another folder"
    // copy; a write failure or a dropped connection is not the path's fault.
    onError: (err: unknown) => setFolderRootError({
      message: apiErrorCode(err) === 'invalid_session_folder_root'
        ? i18nT('pages.settings.chatPanel.session_folder_root_invalid')
        : i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config'),
      report: reportForError(err),
    }),
  })
  const commitFolderRoot = () => {
    if (!folderRootEditedRef.current) return
    const next = localFolderRoot.trim()
    if (next === serverFolderRoot) {
      folderRootEditedRef.current = false
      setFolderRootError(null)
      return
    }
    folderRootMut.mutate(next)
  }

  // ── Compaction wait budget ──
  // The server clamps an out-of-range value instead of refusing it (0 keeps
  // the built-in budget; anything else lands in 60-3600 s), so the field shows
  // what was saved and says why it differs from what was typed.
  const [localWaitSecs, setLocalWaitSecs] = useState('')
  const [waitClampNotice, setWaitClampNotice] = useState('')
  // True only while the user holds a typed, unsaved value. An untouched field
  // follows the server on every refetch, and a blur without an edit saves
  // nothing, so a value saved elsewhere is never written back over.
  const waitSecsEditedRef = useRef(false)
  const serverWaitSecs = mcQ.data?.session?.compact_wait_secs
  useEffect(() => {
    if (mcQ.isSuccess && !waitSecsEditedRef.current) {
      setLocalWaitSecs(String(serverWaitSecs ?? 0))
    }
  }, [mcQ.isSuccess, serverWaitSecs])

  const waitSecsMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('session.compact_wait_secs', n),
    onSuccess: (data: { clamp_notice?: ClampNotice } | undefined) => {
      const notice = data?.clamp_notice
      if (notice) {
        setLocalWaitSecs(String(notice.stored))
        setWaitClampNotice(
          i18nT('pages.settings.chatPanel.compaction_wait_budget_clamped', {
            requested: fmtUnit(notice.requested, 'second'),
            stored: fmtUnit(notice.stored, 'second'),
            min: fmtUnit(notice.min, 'second'),
            max: fmtUnit(notice.max, 'second'),
          }),
        )
      } else {
        setWaitClampNotice('')
      }
      return qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => {
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_compaction_wait_budget'))
      setLocalWaitSecs(String(mcCfg?.session?.compact_wait_secs ?? 0))
    },
  })

  // ── Auto-compact threshold (session.autocompact_pct) ──
  // The select offers presets plus "Custom…"; a stored value off the presets, or
  // a pick of "Custom…", shows a typed field. Same follow-the-server rule as the
  // fields above. The server REFUSES a value outside 5-90 rather than clamping it,
  // so the field clamps first and says what it saved.
  const serverCompactPct = mcQ.data?.session?.autocompact_pct ?? AUTOCOMPACT_PCT_DEFAULT
  const [compactCustomOpen, setCompactCustomOpen] = useState(false)
  const [localCompactPct, setLocalCompactPct] = useState('')
  const [compactClampNotice, setCompactClampNotice] = useState('')
  const compactPctEditedRef = useRef(false)
  useEffect(() => {
    if (mcQ.isSuccess && !compactPctEditedRef.current) setLocalCompactPct(String(serverCompactPct))
  }, [mcQ.isSuccess, serverCompactPct])
  // A settings deep link (search, a setting chip) to the exact field opens
  // Custom… without saving, so the probe finds the field instead of nothing.
  // The selector is the label one useSettingHighlight announces for this row.
  const deepLink = useSettingsDeepLinkTarget()
  const exactFieldSelector = settingAnchorSelector('data-setting-label', i18nT('pages.settings.chatPanel.auto_compact_exact'))
  useEffect(() => {
    if (deepLink === exactFieldSelector) setCompactCustomOpen(true)
  }, [deepLink, exactFieldSelector])
  const compactSelect = compactCustomOpen ? COMPACT_CUSTOM : compactSelectValue(serverCompactPct)

  const pct = (v: number) => fmtPercent(v / 100, { maximumFractionDigits: 2 })
  const clampNoticeFor = (typed: number, n: number) => n === typed ? '' : i18nT(
    'pages.settings.chatPanel.auto_compact_threshold_clamped',
    { requested: pct(typed), stored: pct(n), min: pct(AUTOCOMPACT_PCT_MIN), max: pct(AUTOCOMPACT_PCT_MAX) },
  )
  // `typed` is what the user entered; `n` is the clamped value sent. The "Saved"
  // notice waits for the server, and a retry clears only this row's own failure.
  const compactPctMut = useMutation({
    // One scope for preset picks and typed saves: they run in order, so a
    // pending typed save can never land after a later preset pick.
    scope: { id: AUTOCOMPACT_PATH },
    mutationFn: ({ n }: { n: number; typed: number }) => api.patchConfig(AUTOCOMPACT_PATH, n),
    onMutate: () => {
      setCompactClampNotice('')
      clearOwnPathError(AUTOCOMPACT_PATH)
    },
    onSuccess: (_data, { n, typed }) => {
      clearOwnPathError(AUTOCOMPACT_PATH)
      setCompactClampNotice(clampNoticeFor(typed, n))
      return qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => {
      setPathSaveError(AUTOCOMPACT_PATH, i18nT('pages.settings.chatPanel.failed_to_save_auto_compact_threshold'))
      setLocalCompactPct(String(serverCompactPct))
    },
  })
  const pickCompact = (v: string) => {
    if (v === COMPACT_CUSTOM) {
      setCompactCustomOpen(true)
      return
    }
    setCompactCustomOpen(false)
    setCompactClampNotice('')
    compactPctEditedRef.current = false
    compactPctMut.mutate({ n: Number(v), typed: Number(v) })
  }
  const editCompactPct = (v: string) => {
    // Typing pins the row on Custom…: a value that lands on a preset (95 clamped
    // to 90, or a typed 80) must not unmount the field and its notice.
    setCompactCustomOpen(true)
    compactPctEditedRef.current = true
    setLocalCompactPct(v)
    setCompactClampNotice('')
  }
  const commitCompactPct = () => {
    if (!compactPctEditedRef.current) return
    compactPctEditedRef.current = false
    const typed = Number(localCompactPct)
    if (localCompactPct.trim() === '' || !Number.isFinite(typed)) {
      setLocalCompactPct(String(serverCompactPct))
      return
    }
    const n = clampAutocompactPct(typed)
    setLocalCompactPct(String(n))
    // Already stored: nothing to send, and the stored value is what "Saved" names.
    if (n === serverCompactPct) setCompactClampNotice(clampNoticeFor(typed, n))
    else compactPctMut.mutate({ n, typed })
  }

  const keepModeMut = useMutation({
    mutationFn: (v: CompletionKeepMode) => api.patchConfig('agent.completion_keep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_completion_keep_mode')),
  })
  return {
    localKeepChars, setLocalKeepChars, keepCharsMut, localTurnLimit, setLocalTurnLimit,
    turnLimitEditedRef, turnLimitMut, localFolderRoot, setLocalFolderRoot, folderRootError,
    setFolderRootError, folderRootSaved, setFolderRootSaved, folderRootErrorId,
    folderRootEditedRef, folderRootIme, folderRootMut, commitFolderRoot, localWaitSecs,
    setLocalWaitSecs, waitClampNotice, setWaitClampNotice, waitSecsEditedRef, waitSecsMut,
    keepModeMut, compactSelect, pickCompact, localCompactPct, editCompactPct, commitCompactPct,
    compactClampNotice, compactPctMut,
  }
}
