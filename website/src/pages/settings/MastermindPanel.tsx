import { useId, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Brain, Download } from 'lucide-react'

import { SettingsSection, SettingsCard, SettingsField } from '../../components/settings'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn, Checkbox, ContentSkeleton, Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { api } from '../../api/client'
import { MASTERMIND_QUERY_KEY, MASTERMIND_VIEW_QUERY_KEY, useMastermindInfo } from '../../hooks/useMastermind'
import { useAppDispatch } from '../../store'
import { fetchSlots } from '../../store/dashboardSlice'
import { fmtNumber, fmtUnit } from '../../i18n/format'
import { downloadBlob } from '../../utils/download'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import { MastermindMemoryBrowser } from './MastermindMemoryBrowser'
import { MASTERMIND_SUMMARY_MODEL_PATH, useMastermindSummaryModel } from './useMastermindSummaryModel'

/** How often the stats and the memory view re-read while this page is shown.
 *  React Query pauses the poll while the browser tab is hidden. */
const REFRESH_MS = 10_000

/** Which action's failure the shared notice reports (its title names the action). */
type FailedAction = 'toggle' | 'export' | 'clear' | 'remove'

/**
 * Settings > Mastermind: the switch, the memory tree's size, a browser over the
 * memory view, export, and the destructive pair (clear the memory, remove the
 * session). Its tab is listed while the `mc-preview-mastermind` Feature Previews
 * flag is on (SettingsPage.tsx), the same ingress gate the Webhooks tab uses.
 *
 * Every write is a backend action, not a config path: enabling creates and pins
 * the `mastermind` slot SERVER-side, disabling keeps the chat and its memory. So
 * the switch is a bare `Toggle` in a row rather than a `SettingsToggle` (there is
 * no `configKey` to name), and this file is waived from the settings search index
 * in settingsCoverage.test.ts; the Feature Previews card is the indexed entry.
 */
export function MastermindPanel() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const { confirm, confirmDialog } = useConfirm()
  const enabledDescId = useId()
  const summaryModelId = useId()
  const summaryModel = useMastermindSummaryModel()
  const [failure, setFailure] = useState<{ action: FailedAction; message: string } | null>(null)

  const info = useMastermindInfo({ refreshMs: REFRESH_MS })
  const view = useQuery({
    queryKey: MASTERMIND_VIEW_QUERY_KEY,
    queryFn: () => api.mastermindView(),
    refetchInterval: REFRESH_MS,
    staleTime: REFRESH_MS / 2,
  })

  /** After a write: re-read the status, the view and every zoom (one prefix), and
   *  the slots, since enable/remove create or delete the session server-side. */
  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: MASTERMIND_QUERY_KEY })
    void dispatch(fetchSlots())
  }
  const failWith = (action: FailedAction) => (err: unknown) =>
    setFailure({ action, message: errMessage(err) || i18nT('pages.settings.mastermindPanel.action_failed') })

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setMastermindEnabled(enabled),
    onMutate: () => setFailure(null),
    onSuccess: refreshAll,
    onError: failWith('toggle'),
  })
  const exportMemory = useMutation({
    mutationFn: () => api.mastermindExport(),
    onMutate: () => setFailure(null),
    onSuccess: ({ blob, filename }) => downloadBlob(blob, filename || 'mastermind-memory.json'),
    onError: failWith('export'),
  })
  const clear = useMutation({
    mutationFn: () => api.clearMastermind(),
    onMutate: () => setFailure(null),
    onSuccess: refreshAll,
    onError: failWith('clear'),
  })
  const remove = useMutation({
    mutationFn: (forget: boolean) => api.removeMastermind(forget),
    onMutate: () => setFailure(null),
    onSuccess: refreshAll,
    onError: failWith('remove'),
  })

  const askClear = async () => {
    const ok = await confirm({
      title: i18nT('pages.settings.mastermindPanel.clear_confirm_title'),
      body: i18nT('pages.settings.mastermindPanel.clear_confirm_body'),
      confirmLabel: i18nT('pages.settings.mastermindPanel.clear'),
    })
    if (ok) clear.mutate()
  }
  // The checkbox lives in the dialog body, which `confirm` captures once, so its
  // value is read back through a ref after the answer arrives.
  const forgetRef = useRef(false)
  const askRemove = async () => {
    forgetRef.current = false
    const ok = await confirm({
      title: i18nT('pages.settings.mastermindPanel.remove_confirm_title'),
      body: (
        <div className="flex flex-col gap-3">
          <p className="m-0">{i18nT('pages.settings.mastermindPanel.remove_confirm_body')}</p>
          <label className="flex items-center gap-2 cursor-pointer">
            <Checkbox data-testid="mastermind-remove-forget" defaultChecked={false}
              onChange={e => { forgetRef.current = e.currentTarget.checked }} />
            <span>{i18nT('pages.settings.mastermindPanel.remove_forget')}</span>
          </label>
        </div>
      ),
      confirmLabel: i18nT('pages.settings.mastermindPanel.remove'),
    })
    if (ok) remove.mutate(forgetRef.current)
  }

  const data = info.data
  const enabled = data?.enabled === true
  const failureTitle = failure && {
    toggle: i18nT('pages.settings.mastermindPanel.toggle_failed'),
    export: i18nT('pages.settings.mastermindPanel.export_failed'),
    clear: i18nT('pages.settings.mastermindPanel.clear_failed'),
    remove: i18nT('pages.settings.mastermindPanel.remove_failed'),
  }[failure.action]

  return (
    <SettingsSection title={i18nT('settings.tabs.mastermind.label')}>
      {confirmDialog}
      <SettingsCard>
        <div className="flex items-start gap-2">
          <Brain size={16} className="text-accent shrink-0 mt-0.5" aria-hidden="true" />
          <p className="text-[13px] text-muted m-0">{i18nT('pages.settings.mastermindPanel.intro')}</p>
        </div>
        <div className="flex items-center justify-between gap-4 pt-3">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.mastermindPanel.enabled')}</div>
            <div id={enabledDescId} className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.mastermindPanel.enabled_desc')}</div>
          </div>
          <Toggle
            checked={enabled}
            disabled={!data || toggle.isPending}
            label={i18nT('pages.settings.mastermindPanel.enabled')}
            describedBy={enabledDescId}
            onChange={v => toggle.mutate(v)}
          />
        </div>
        {/* The summaries' own model, apart from the chat's: the same options, labels
            and optimistic save as the Chat panel's per-role model rows. A bare
            SimpleSelect in a SettingsField (not SettingsSelect) because this tab is
            preview-gated and kept out of the settings search index. */}
        <div className="pt-3">
          <SettingsField label={i18nT('pages.settings.mastermindPanel.summary_model')}
            description={i18nT('pages.settings.mastermindPanel.summary_model_desc')}
            hint={i18nT('pages.settings.chatPanel.role_model_auto_hint')}
            configKey={MASTERMIND_SUMMARY_MODEL_PATH} controlId={summaryModelId}>
            <SimpleSelect
              id={summaryModelId}
              options={summaryModel.options}
              optionLabels={summaryModel.labels}
              value={summaryModel.shown}
              onChange={v => summaryModel.set(v)}
              disabled={!summaryModel.ready}
              aria-label={i18nT('pages.settings.mastermindPanel.summary_model')}
              triggerFallback={summaryModel.labelOf(summaryModel.shown)}
            />
          </SettingsField>
          <ErrorNotice
            title={i18nT('pages.settings.mastermindPanel.summary_model_failed')}
            message={summaryModel.error || null}
            askAgent
            onDismiss={summaryModel.dismissError}
            testId="mastermind-summary-model-error"
            className="mt-1"
          />
        </div>
      </SettingsCard>

      <ErrorNotice
        title={failureTitle ?? undefined}
        message={failure?.message ?? null}
        askAgent
        onDismiss={() => setFailure(null)}
        testId="mastermind-action-error"
        className="mb-2"
      />

      <SettingsCard>
        <div className="text-[13px] font-semibold text-text mb-2">{i18nT('pages.settings.mastermindPanel.stats_title')}</div>
        {info.isPending ? <ContentSkeleton rows={2} /> : info.error ? (
          <ErrorNotice variant="inline" title={i18nT('pages.settings.mastermindPanel.stats_failed')}
            message={errMessage(info.error)} askAgent testId="mastermind-stats-error" />
        ) : (
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 m-0" data-testid="mastermind-stats">
            <Stat label={i18nT('pages.settings.mastermindPanel.stat_messages')} value={fmtNumber(data?.messages ?? 0)} />
            <Stat label={i18nT('pages.settings.mastermindPanel.stat_view_size')}
              value={fmtUnit((data?.view_bytes ?? 0) / 1000, 'kilobyte', { maximumFractionDigits: 1 })} />
            <Stat label={i18nT('pages.settings.mastermindPanel.stat_lines')} value={fmtNumber(data?.view_lines ?? 0)} />
            <Stat label={i18nT('pages.settings.mastermindPanel.stat_pending')} value={fmtNumber(data?.pending_summaries ?? 0)} />
          </dl>
        )}
      </SettingsCard>

      <SettingsCard>
        <div className="flex items-center justify-between gap-3 mb-1">
          <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.mastermindPanel.browser_title')}</div>
          <Btn onClick={() => exportMemory.mutate()} disabled={exportMemory.isPending} data-testid="mastermind-export">
            <Download size={13} className="lucide-inline" aria-hidden="true" /> {i18nT('pages.settings.mastermindPanel.export')}
          </Btn>
        </div>
        <p className="text-[12px] text-muted mt-0 mb-2">{i18nT('pages.settings.mastermindPanel.browser_hint')}</p>
        {view.isPending ? <ContentSkeleton rows={3} /> : view.error ? (
          <ErrorNotice variant="inline" title={i18nT('pages.settings.mastermindPanel.browse_failed')}
            message={errMessage(view.error)} askAgent testId="mastermind-view-error" />
        ) : (
          <div className="max-h-[420px] overflow-y-auto">
            <MastermindMemoryBrowser lines={view.data?.lines ?? []} />
          </div>
        )}
      </SettingsCard>

      <SettingsCard>
        <div className="text-[13px] font-semibold text-danger mb-2">{i18nT('pages.settings.mastermindPanel.danger_title')}</div>
        <div className="flex items-center justify-between gap-4 py-1.5">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.mastermindPanel.clear')}</div>
            <div className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.mastermindPanel.clear_desc')}</div>
          </div>
          <Btn danger onClick={() => { void askClear() }} disabled={clear.isPending} data-testid="mastermind-clear">
            {i18nT('pages.settings.mastermindPanel.clear')}
          </Btn>
        </div>
        <div className="flex items-center justify-between gap-4 py-1.5">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.mastermindPanel.remove')}</div>
            <div className="text-[12px] text-muted mt-0.5">{i18nT('pages.settings.mastermindPanel.remove_desc')}</div>
          </div>
          <Btn danger onClick={() => { void askRemove() }} disabled={remove.isPending || (!!data && !data.enabled && !data.slot_exists)}
            data-testid="mastermind-remove">
            {i18nT('pages.settings.mastermindPanel.remove')}
          </Btn>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-muted">{label}</dt>
      <dd className="m-0 text-[15px] font-semibold text-text-strong tabular-nums">{value}</dd>
    </div>
  )
}
