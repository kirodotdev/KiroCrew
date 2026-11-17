/**
 * Settings ▸ Chat ▸ Sessions ▸ Sessions per creator (`session.max_per_creator`).
 *
 * A typed whole number. The field follows the saved value until it is edited and
 * saves on blur or Enter. A number outside the range is saved as the nearest
 * bound, and the row says which number it saved. The gateway reads the value at
 * each session create, so a save applies with no restart.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsInput } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { parseWholeNumber } from '../../utils/wholeNumber'
import { FieldHint } from './ChatPanelLinkPatternsEditor'
import {
  type KirocrewConfigShape, SESSIONS_PER_CREATOR_DEFAULT, SESSIONS_PER_CREATOR_MAX,
  SESSIONS_PER_CREATOR_MIN, SESSIONS_PER_CREATOR_PATH,
} from './chatPanelOptions'

export function SessionsPerCreatorSetting() {
  const qc = useQueryClient()
  const config = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const saved = config.data?.session?.max_per_creator ?? SESSIONS_PER_CREATOR_DEFAULT
  const [draft, setDraft] = useState('')
  const [clampNotice, setClampNotice] = useState('')
  // True only while the field holds a typed, unsaved number. An untouched field
  // follows every refetch, and a blur without an edit saves nothing.
  const editedRef = useRef(false)
  useEffect(() => {
    if (config.isSuccess && !editedRef.current) setDraft(String(saved))
  }, [config.isSuccess, saved])

  const clampNoticeFor = (typed: number, n: number) => n === typed ? '' : i18nT(
    'pages.settings.chatPanel.auto_compact_threshold_clamped',
    {
      requested: fmtNumber(typed),
      stored: fmtNumber(n),
      min: fmtNumber(SESSIONS_PER_CREATOR_MIN),
      max: fmtNumber(SESSIONS_PER_CREATOR_MAX),
    },
  )

  // `typed` is what the owner entered; `n` is the clamped number sent. The
  // "Saved" notice waits for the server to accept `n`.
  const save = useMutation({
    mutationFn: ({ n }: { n: number; typed: number }) => api.patchConfig(SESSIONS_PER_CREATOR_PATH, n),
    onSuccess: (_data, { n, typed }) => {
      setClampNotice(clampNoticeFor(typed, n))
      return qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    // The typed number stays on screen and marked edited, so the next blur retries it.
    onError: (_err, { n }) => {
      editedRef.current = true
      setDraft(String(n))
    },
  })

  const commit = () => {
    if (!editedRef.current) return
    editedRef.current = false
    const typed = parseWholeNumber(draft)
    if (typed === null) {
      setDraft(String(saved))
      return
    }
    const n = Math.min(SESSIONS_PER_CREATOR_MAX, Math.max(SESSIONS_PER_CREATOR_MIN, typed))
    setDraft(String(n))
    // Nothing to send when the clamped number is already saved, so the notice
    // shows at once; otherwise it waits for the save to succeed.
    if (n === saved) setClampNotice(clampNoticeFor(typed, n))
    else save.mutate({ n, typed })
  }

  return (
    <>
      <SettingsInput
        label={i18nT('pages.settings.chatPanel.sessions_per_creator')}
        description={i18nT('pages.settings.chatPanel.sessions_per_creator_desc')}
        hint={i18nT('pages.settings.chatPanel.sessions_per_creator_hint')}
        type="number"
        value={draft}
        min={SESSIONS_PER_CREATOR_MIN}
        max={SESSIONS_PER_CREATOR_MAX}
        step={10}
        onChange={v => {
          editedRef.current = true
          setDraft(v)
          setClampNotice('')
        }}
        onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit() }}
        // Locked while a save is in flight: its result must not land on a newer draft.
        disabled={!config.isSuccess || save.isPending}
        configKey="session.max_per_creator"
      />
      <FieldHint message={clampNotice} />
      {/* No hand-off: the typed number is an unsaved draft in this field, and
          leaving the page to ask the agent would discard it. */}
      <ErrorNotice
        variant="inline"
        message={save.isError ? i18nT('pages.settings.chatPanel.failed_to_save_sessions_per_creator') : null}
      />
    </>
  )
}
