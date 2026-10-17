/**
 * Settings ▸ Chat's typed number and path fields: completion-event characters, the
 * turn time limit, the new-session folder and the compaction wait budget. Each holds a
 * local draft that follows the server until edited and saves on blur or Enter; the
 * completion-keep select's save sits with them in the Subagents card. `ChatPanel`
 * calls it once per render, between its two Model-page hooks.
 */
import { useState, useRef, useEffect, useId } from 'react'
import { useMutation, type QueryClient, type UseQueryResult } from '@tanstack/react-query'
import type { DashboardConfig } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
import { apiErrorCode } from '../../api/apiError'
import { reportForError, type ErrorReport } from '../../utils/errorReport'
import {
  TURN_LIMIT_DEFAULT, type CompletionKeepMode, COMPLETION_KEEP_CHARS_DEFAULT, type ClampNotice,
  type KirocrewConfigShape,
} from './chatPanelOptions'

export function useChatPanelTypedFields({
  qc, setSaveError, dashQ, mcQ, mcCfg,
}: {
  qc: QueryClient
  setSaveError: (msg: string) => void
  dashQ: UseQueryResult<DashboardConfig>
  mcQ: UseQueryResult<KirocrewConfigShape>
  mcCfg: KirocrewConfigShape | undefined
}) {
  const [localKeepChars, setLocalKeepChars] = useState('')
  const keepCharsInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !keepCharsInitRef.current) {
      keepCharsInitRef.current = true
      setLocalKeepChars(String(mcQ.data.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT))
    }
  }, [mcQ.data])

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
    keepModeMut,
  }
}
