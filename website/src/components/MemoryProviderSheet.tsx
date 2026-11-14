import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import { MEMORY_MOVE_ACTIVE_STATES, type MemoryProviderOverview, type MemoryProviderRow } from '../api/client/memory'
import {
  registerMemoryProviderSheetHost,
  type MemoryProviderChangeOutcome,
  type MemoryProviderChangeTarget,
} from '../app-sdk/memoryProviderChange'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { Btn, Checkbox } from './ui'
import { i18nT } from '../i18n/t'

/** The React Query key every memory provider surface reads and refreshes. */
export const MEMORY_PROVIDER_QUERY_KEY = ['memory-provider'] as const

/** The provider a target names, from the overview. */
export function providerRow(
  overview: MemoryProviderOverview | undefined,
  id: string | undefined,
): MemoryProviderRow | undefined {
  return id ? overview?.providers.find(p => p.id === id) : undefined
}

/** The display name of whichever memory *id* names. */
export function providerName(overview: MemoryProviderOverview | undefined, id: string | undefined): string {
  return providerRow(overview, id)?.display_name ?? i18nT('pages.overview.memoryProvider.builtin_name')
}

/** The sentence for a refused switch, by the gateway's code. */
function refusal(e: unknown): string {
  switch (apiErrorCode(e)) {
    case 'replace_unsupported':
      return i18nT('pages.overview.memoryProvider.error_replace_unsupported')
    case 'augment_unsupported':
      return i18nT('pages.overview.memoryProvider.error_augment_unsupported')
    case 'egress_consent_required':
      return i18nT('pages.overview.memoryProvider.error_egress_consent')
    case 'stop_learning_ack_required':
      return i18nT('pages.overview.memoryProvider.error_stop_learning')
    case 'member_stores_must_stay_builtin':
      return i18nT('pages.overview.memoryProvider.error_member_stores')
    case 'shared_crew':
      return i18nT('pages.overview.memoryProvider.error_shared_crew')
    case 'policy_denied':
      return i18nT('pages.overview.memoryProvider.error_policy')
    case 'owner_only':
      return i18nT('pages.overview.memoryProvider.error_owner_only')
    default:
      return i18nT('pages.overview.memoryProvider.error_switch_failed')
  }
}

/**
 * The confirmation sheet for one switch. The owner's press is the switch: it calls
 * `PUT /api/memory/provider`, which refuses every app token, then optionally moves
 * memory into the new provider as a move job (nothing is deleted on either side).
 */
export function MemoryProviderSheet({
  target,
  onDone,
}: {
  target: MemoryProviderChangeTarget
  onDone: (outcome: MemoryProviderChangeOutcome) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const overview = useQuery({ queryKey: MEMORY_PROVIDER_QUERY_KEY, queryFn: () => api.memoryProvider() })
  const data = overview.data
  const row = providerRow(data, target.id)
  const caps = row?.capabilities
  const needsEgress = !!caps && caps.egress === 'remote' && !row?.egress_consented
  const needsStopAck = target.mode === 'replace' && caps?.ingest === 'none'
  const needsPin = !!caps && !caps.isolation.multi_store
  const leavingReplace = target.mode === 'builtin' && data?.mode === 'replace' && !!data.id
  const copyFrom = target.mode === 'replace' ? 'builtin' : leavingReplace ? data?.id ?? '' : ''
  const copyTo = target.mode === 'replace' ? target.id ?? '' : leavingReplace ? 'builtin' : ''
  const [egress, setEgress] = useState(false)
  const [stopAck, setStopAck] = useState(false)
  const [pin, setPin] = useState(false)
  const [copy, setCopy] = useState(true)
  // The switch is done once the PUT answers. A copy then runs as a job the sheet
  // follows to its end; a copy that fails is reported on its own, so the sheet never
  // calls a switch that happened a failure, and closing it still answers 'switched'.
  const [copyJobId, setCopyJobId] = useState('')
  const [copyStartFailed, setCopyStartFailed] = useState(false)
  const switchMut = useMutation({
    mutationFn: async (): Promise<string | null> => {
      await api.switchMemoryProvider({
        mode: target.mode,
        ...(target.id ? { id: target.id } : {}),
        ...(needsEgress ? { egress_consent: egress } : {}),
        ...(needsStopAck ? { stop_learning_ack: stopAck } : {}),
        ...(needsPin ? { pin_member_stores_builtin: pin } : {}),
      })
      if (!copy || !copyFrom || !copyTo) return ''
      try {
        return (await api.startMemoryMove({ from: copyFrom, to: copyTo })).id
      } catch {
        return null
      }
    },
    onSuccess: jobId => {
      void queryClient.invalidateQueries({ queryKey: MEMORY_PROVIDER_QUERY_KEY })
      if (jobId === '') onDone('switched')
      else if (jobId === null) setCopyStartFailed(true)
      else setCopyJobId(jobId)
    },
  })
  const copyJob = useQuery({
    queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'copy', copyJobId],
    queryFn: () => api.memoryMove(copyJobId),
    enabled: !!copyJobId,
    refetchInterval: query =>
      query.state.data && !MEMORY_MOVE_ACTIVE_STATES.has(query.state.data.state) ? false : 1000,
  })
  const job = copyJob.data
  // A refused or erroring item was not copied; the owner is told, never a silent close.
  const copyErrors = job
    ? Object.entries(job.outcomes).some(([k, n]) => n > 0 && (k.startsWith('error:') || k.startsWith('refused')))
    : false
  const copyPartial = job?.state === 'done' && copyErrors
  const copyFailed =
    copyStartFailed ||
    copyJob.isError ||
    job?.state === 'failed' ||
    job?.state === 'paused' ||
    job?.state === 'cancelled' ||
    (job?.state === 'done' && copyErrors)
  const copyRunning = !!copyJobId && !copyFailed && (!job || MEMORY_MOVE_ACTIVE_STATES.has(job.state))
  useEffect(() => {
    if (job?.state === 'done' && !copyErrors) onDone('switched')
  }, [job?.state, copyErrors, onDone])
  const switched = copyStartFailed || !!copyJobId
  const close = () => onDone(switched ? 'switched' : 'cancelled')
  const name = providerName(data, target.id)
  const fromName = providerName(data, copyFrom === 'builtin' ? undefined : copyFrom)
  const leavingName = providerName(data, data?.mode === 'builtin' ? undefined : data?.id)
  const blocked = (needsEgress && !egress) || (needsStopAck && !stopAck) || (needsPin && !pin)
  const stoppingAugment = target.mode === 'builtin' && data?.mode === 'augment'
  const title =
    stoppingAugment
      ? t('pages.overview.memoryProvider.sheet_title_stop_augment', { name: leavingName })
      : target.mode === 'builtin'
      ? t('pages.overview.memoryProvider.sheet_title_builtin')
      : target.mode === 'replace'
        ? t('pages.overview.memoryProvider.sheet_title_replace', { name })
        : t('pages.overview.memoryProvider.sheet_title_augment', { name })
  return (
    <Modal
      open
      onClose={close}
      title={title}
      maxWidth={520}
      layer="top"
      dismissDisabled={switchMut.isPending}
      footer={
        <div className="flex justify-end gap-2">
          {switched ? (
            <Btn primary onClick={close}>
              {t('pages.overview.memoryProvider.close')}
            </Btn>
          ) : (
            <>
              <Btn onClick={close} disabled={switchMut.isPending}>
                {t('pages.overview.memoryProvider.cancel')}
              </Btn>
              <Btn primary busy={switchMut.isPending} disabled={blocked || !overview.isSuccess} onClick={() => switchMut.mutate()}>
                {t('pages.overview.memoryProvider.confirm')}
              </Btn>
            </>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        <p>
          {stoppingAugment
            ? t('pages.overview.memoryProvider.sheet_body_stop_augment', { name: leavingName })
            : target.mode === 'builtin'
            ? t('pages.overview.memoryProvider.sheet_body_builtin', { name: leavingName })
            : target.mode === 'replace'
              ? t('pages.overview.memoryProvider.sheet_body_replace', { name })
              : t('pages.overview.memoryProvider.sheet_body_augment', { name })}
        </p>
        <p className="text-muted">{t('pages.overview.memoryProvider.sheet_new_chats')}</p>
        {needsEgress && (
          <label className="flex items-start gap-2">
            <Checkbox checked={egress} onChange={e => setEgress(e.target.checked)} />
            <span>{t('pages.overview.memoryProvider.egress_consent', { name })}</span>
          </label>
        )}
        {needsStopAck && (
          <label className="flex items-start gap-2">
            <Checkbox checked={stopAck} onChange={e => setStopAck(e.target.checked)} />
            <span>{t('pages.overview.memoryProvider.stop_learning_ack', { name })}</span>
          </label>
        )}
        {needsPin && (
          <label className="flex items-start gap-2">
            <Checkbox checked={pin} onChange={e => setPin(e.target.checked)} />
            <span>{t('pages.overview.memoryProvider.pin_member_stores', { name })}</span>
          </label>
        )}
        {copyFrom && copyTo && (
          <label className="flex items-start gap-2">
            <Checkbox checked={copy} onChange={e => setCopy(e.target.checked)} />
            <span>
              {target.mode === 'replace'
                ? t('pages.overview.memoryProvider.copy_in', { name })
                : t('pages.overview.memoryProvider.copy_back', { name: fromName })}
            </span>
          </label>
        )}
        {/* No hand-off: the sheet holds the owner's unsent consent choices, which need this read. */}
        {overview.isError && (
          <ErrorNotice variant="inline" message={t('pages.overview.memoryProvider.load_failed')} />
        )}
        {/* No hand-off: the sheet holds the owner's unsent consent choices. */}
        {switchMut.isError && <ErrorNotice variant="inline" message={refusal(switchMut.error)} />}
        {copyRunning && <p className="text-muted">{t('pages.overview.memoryProvider.copy_running')}</p>}
        {/* The switch is saved by now and no choice is left unsent, so the agent can pick
            up a failed copy. */}
        {copyFailed && (
          <ErrorNotice
            variant="inline"
            askAgent
            message={t(copyPartial ? 'pages.overview.memoryProvider.copy_partial' : 'pages.overview.memoryProvider.copy_failed')}
          />
        )}
      </div>
    </Modal>
  )
}

/**
 * Mounts the sheet `requestMemoryProviderChange()` opens. One per window, beside
 * the other host dialogs; an ask that arrives while one is open replaces nothing
 * and answers `'cancelled'`.
 */
export function MemoryProviderSheetHost() {
  const [target, setTarget] = useState<MemoryProviderChangeTarget | null>(null)
  const settle = useRef<((outcome: MemoryProviderChangeOutcome) => void) | null>(null)
  useEffect(() => {
    const off = registerMemoryProviderSheetHost(next => new Promise(resolve => {
      if (settle.current) {
        resolve('cancelled')
        return
      }
      settle.current = resolve
      setTarget(next)
    }))
    return () => {
      off()
      settle.current?.('cancelled')
      settle.current = null
    }
  }, [])
  if (!target) return null
  return (
    <MemoryProviderSheet
      target={target}
      onDone={outcome => {
        settle.current?.(outcome)
        settle.current = null
        setTarget(null)
      }}
    />
  )
}
