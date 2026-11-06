/** Ultra Mode "Pause all" / "Resume all" on the Board's Workers list.
 *
 *  `CrewPauseRow` is the one control row under the search bar: while the crew
 *  runs it counts what runs and what needs the person and offers Pause all
 *  (behind a confirm); while paused it is the blue "Paused" row with Resume all.
 *  `CrewMemberContinue` is a stopped member's own Continue. Each calls an existing
 *  route (`.../crew-workers/pause`, `.../resume`, the slot's `/continue`) and then
 *  re-reads the worker list, which carries the pause (`paused`). */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pause, Play } from 'lucide-react'
import { api } from '../../../api/client'
import type { CrewPause } from '../../../types'
import ErrorNotice from '../../../components/ErrorNotice'
import { useConfirm } from '../../../components/ConfirmDialog'
import { i18nT } from '../../../i18n/t'
import { crewWorkersQueryKey } from './CrewWorkerList'

const BTN = 'shrink-0 inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md border text-[12px] font-semibold disabled:opacity-50 disabled:cursor-default'

export function CrewPauseRow({ root, paused, running, needsYou, leadRunning = false }: {
  root: string
  paused: CrewPause | undefined
  /** Running workers; the lead's own turn is `leadRunning`. */
  running: number
  needsYou: number
  leadRunning?: boolean
}) {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<'pause' | 'resume' | null>(null)
  const act = async (which: 'pause' | 'resume') => {
    if (which === 'pause' && !await confirm({
      title: i18nT('components.crewMode.ultra_pause_confirm_title'),
      body: running > 0
        ? i18nT('components.crewMode.ultra_pause_confirm_body', { count: running })
        : i18nT('components.crewMode.ultra_pause_confirm_lead'),
      confirmLabel: i18nT('components.crewMode.ultra_pause_all'),
      primary: true,
    })) return
    setBusy(true)
    setFailed(null)
    try {
      if (which === 'pause') await api.crewPause(root)
      // A session the resume could not hand back stays paused, and says so.
      else if ((await api.crewResume(root)).failed?.length) setFailed('resume')
    } catch {
      setFailed(which)
    } finally {
      setBusy(false)
      void queryClient.invalidateQueries({ queryKey: crewWorkersQueryKey(root) })
    }
  }
  // Offered while the lead alone works too: only Pause all holds the whole crew
  // (its sends, patrol wakes and new sessions), which the lead's Stop does not.
  if (!paused && running <= 0 && !leadRunning) return null
  const counts = running > 0
    ? [i18nT('components.crewMode.ultra_row_count_running', { count: running })]
    : [i18nT('components.crewMode.ultra_row_lead_working')]
  if (needsYou > 0) counts.push(i18nT('components.crewMode.ultra_row_count_needs_you', { count: needsYou }))
  return (
    <div className="shrink-0 px-3 pt-2" data-testid="crew-pause-row" data-paused={paused ? 'true' : undefined}>
      <div className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 ${paused ? 'crew-paused-row' : 'border-border bg-bg-elevated/40'}`}>
        {paused && <Pause size={13} aria-hidden="true" className="shrink-0 crew-tone-paused" />}
        <span className="flex-1 min-w-0 text-[12px] text-muted" data-testid="crew-pause-summary">
          {paused
            ? <><b className="crew-tone-paused font-semibold">{i18nT('components.crewMode.ultra_paused')}</b>{' · '}{i18nT('components.crewMode.ultra_paused_detail', { count: paused.stopped.length })}</>
            : counts.join(' · ')}
        </span>
        {paused ? (
          <button type="button" disabled={busy} onClick={() => void act('resume')} data-testid="crew-resume-all"
            className={`${BTN} ultra-accent border-[color-mix(in_srgb,var(--aim)_55%,var(--border))] bg-[color-mix(in_srgb,var(--aim)_10%,transparent)] hover:bg-[color-mix(in_srgb,var(--aim)_16%,transparent)]`}>
            <Play size={12} aria-hidden="true" fill="currentColor" />{i18nT('components.crewMode.ultra_resume_all')}
          </button>
        ) : (
          <button type="button" disabled={busy} onClick={() => void act('pause')} data-testid="crew-pause-all"
            className={`${BTN} border-border text-text hover:bg-bg-hover`}>
            <Pause size={12} aria-hidden="true" fill="currentColor" />{i18nT('components.crewMode.ultra_pause_all')}
          </button>
        )}
      </div>
      {failed && <div className="pt-1.5"><ErrorNotice variant="inline" askAgent message={i18nT(failed === 'pause' ? 'components.crewMode.ultra_pause_failed' : 'components.crewMode.ultra_resume_failed')} /></div>}
      {confirmDialog}
    </div>
  )
}

/** A stopped member's "Paused" mark and its own Continue (the tab's Continue route). */
export function CrewMemberContinue({ root, worker }: { root: string; worker: string }) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const go = async () => {
    setBusy(true)
    setFailed(false)
    try {
      await api.continueSlot(worker)
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
      void queryClient.invalidateQueries({ queryKey: crewWorkersQueryKey(root) })
    }
  }
  return (
    <span className="shrink-0 flex flex-col items-end gap-1" data-testid="crew-member-paused">
      <span className="flex items-center gap-1.5">
        <span className="inline-flex items-center gap-1 text-[12px] crew-tone-paused">
          <Pause size={11} aria-hidden="true" fill="currentColor" />{i18nT('components.crewMode.ultra_paused')}
        </span>
        <button type="button" disabled={busy} onClick={() => void go()} data-testid="crew-member-continue"
          className="h-6 px-2 rounded-md border border-border text-[11px] text-muted hover:text-text hover:bg-bg-hover disabled:opacity-50">
          {i18nT('components.crewMode.ultra_paused_continue')}
        </button>
      </span>
      {failed && <ErrorNotice variant="inline" askAgent message={i18nT('components.crewMode.ultra_continue_failed')} />}
    </span>
  )
}
