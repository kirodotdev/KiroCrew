/** Switch every live session to one model: the pick, the roster read, the
 *  skip-running choice and the switch write (`useBulkModelSwitch`), and the
 *  header menu's panel that drives them. */
import { useState, useRef, useMemo, useId } from 'react'
import { Cpu } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Btn } from '../../components/ui'
import ModelDropdownList from '../../components/ModelDropdownList'
import SimpleSelect from '../../components/SimpleSelect'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { useListboxKeyboard } from '../../hooks/useListboxKeyboard'
import { partitionBulkSwitch } from '../../lib/bulkModelSwitch'
import { i18nT } from '../../i18n/t'
import { fmtList, fmtNumber } from '../../i18n/format'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import type { Slot } from './types'
import type { SidebarScope } from './scope'

/** The effort choice that leaves each session's effort as it is. Not a level,
 *  so it never reaches the wire. */
export const BULK_EFFORT_KEEP = 'keep'
/** The levels the bulk switch offers. No Default (''): the route refuses it,
 *  because a reset cannot clear kiro-cli's workspace effort overlay, so a
 *  session goes back to its default from its own effort dropdown. */
const BULK_EFFORT_LEVELS = EFFORT_LEVELS.filter(level => level !== '')
/** What the disabled effort select is handed: it matches no row, so the select
 *  shows its dash rather than a choice nobody made. */
const BULK_EFFORT_NONE = 'none'
/** A switch outcome that names no session. */
const NO_OUTCOME: { switched: string[]; busy: string[]; deferred: string[]; failed: string[] } = { switched: [], busy: [], deferred: [], failed: [] }

/** The model switch panel's state, roster and write. */
export function useBulkModelSwitch({ localSlots, slots }: {
  localSlots: Slot[]
  /** Every local live slot, including hidden Crew Mode workers and non-chat-page
   *  member DMs; exclude peer-owned slots. */
  slots: Slot[]
}) {
  // Bulk model switch — apply one model to every live session at once.
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkEffort, setBulkEffort] = useState<string>(BULK_EFFORT_KEEP)
  // The notice, or the last switch's outcome: the slot keys it switched,
  // skipped as busy, deferred to their next start and failed on, and the model
  // and effort it was for. One state, so every caller that clears the notice
  // also forgets the outcome.
  const [bulkError, setBulkError] = useState({ message: '', model: '', effort: undefined as string | undefined, ...NO_OUTCOME })
  const setBulkModelError = (message: string) => setBulkError({ message, model: '', effort: undefined, ...NO_OUTCOME })
  // Per-instance id: SessionsSidebarChrome mounts a mobile-drawer sidebar and a desktop one, so
  // a literal id would collide and point one panel's checkbox at the other's label.
  const bulkSkipRunningLabelId = useId()
  const bulkModelsQuery = useAvailableModelsQuery({ enabled: bulkModelOpen })
  const bulkModelOptions = bulkModelsQuery.data
  // The roster failed to load when EITHER flag is up. The ACP adapter never
  // rejects: a 503 / network error / empty response resolves with the last-good
  // cached list or Auto alone and marks the provider degraded, so `isError`
  // alone would stay false through every real failure and the panel would show
  // a one-entry list as if that were the whole catalog. Same pair Settings >
  // Chat reads for its model selects.
  const bulkModelsFailed = bulkModelsQuery.isError || bulkModelsQuery.isDegraded
  // The pick counts only while the roster still lists it. A degraded roster is
  // the last-good CACHED list, so a model can be picked from it, Retry can then
  // succeed with a roster that no longer carries that model, and nothing else
  // would unpick it: the backend accepts any non-registry id, so Switch would
  // reset every session onto a model kiro-cli then refuses. Derived, not
  // stored, so there is no window between the roster changing and the pick
  // being cleared in which Switch could still fire with the stale id.
  const bulkModelPick = useMemo(
    () => (bulkModelOptions.some(m => m.name === bulkModel) ? bulkModel : ''),
    [bulkModelOptions, bulkModel],
  )
  // Effort is offered only for a model that uses it, and derived like the model
  // pick: moving to a model without effort drops the level instead of sending
  // one that model ignores. undefined = leave each session's effort as it is.
  const bulkEffortSupported = modelSupportsEffort(bulkModelPick)
  const bulkEffortPick = bulkEffortSupported && bulkEffort !== BULK_EFFORT_KEEP ? bulkEffort : undefined
  // Count only slots that would actually change. Every slot left out lands in
  // a named bucket the panel renders (see partitionBulkSwitch), so the
  // "Switch N" label can be reconciled with the rows on screen.
  const bulkPartition = useMemo(
    () => partitionBulkSwitch(localSlots, bulkModelPick, bulkSkipRunning, bulkEffortPick),
    [localSlots, bulkModelPick, bulkSkipRunning, bulkEffortPick],
  )
  const bulkAffectedCount = bulkPartition.affected
  // Whether Switch would send the failed request again: the same model and
  // effort, and exactly the sessions it failed on, including workers hidden
  // from the list.
  const bulkRetriesFailed = useMemo(() => {
    const failed = new Set(bulkError.failed)
    return failed.size > 0 && bulkError.model === bulkModelPick && bulkError.effort === bulkEffortPick
      && partitionBulkSwitch(slots, bulkModelPick, bulkSkipRunning, bulkEffortPick).affected === failed.size
      && partitionBulkSwitch(slots.filter(s => failed.has(s.key)), bulkModelPick, bulkSkipRunning, bulkEffortPick).affected === failed.size
  }, [bulkError, slots, bulkModelPick, bulkSkipRunning, bulkEffortPick])
  // Every notice names its sessions as their titles read now, so a rename or a
  // language switch while the panel is open shows.
  const bulkSessionNames = (keys: readonly string[]) =>
    fmtList(keys.map(key => sessionDisplayTitle(slots.find(s => s.key === key) ?? { key })))
  const bulkModelError = bulkError.failed.length
    ? i18nT('pages.chatSidebar.session_failed_to_switch', { count: bulkError.failed.length, names: bulkSessionNames(bulkError.failed) })
    : bulkError.message
  // After a switch that left something to say, each outcome names its sessions,
  // and each session is in one line only. The panel then stays open; once any
  // session was switched, skipped or deferred its Cancel reads Close, since
  // leaving the panel undoes nothing. A new model or effort pick retires the
  // outcome, which reported the previous one.
  const bulkOutcomeShown = bulkError.model === bulkModelPick && bulkError.effort === bulkEffortPick
    && bulkError.failed.length + bulkError.busy.length + bulkError.deferred.length > 0
  const bulkOutcomeApplied = bulkOutcomeShown && bulkError.switched.length + bulkError.busy.length + bulkError.deferred.length > 0
  // The backend lists a deferred session in `switched` too; its deferred line
  // is the one that says what happened to it.
  const bulkSwitchedNow = bulkError.switched.filter(key => !bulkError.deferred.includes(key))
  // The skipped busy sessions that are idle now and that the Switch count
  // includes, so pressing Switch moves them and stops no reply. Their line sits
  // under the busy one: the one exception to a session being in one line only.
  const bulkBusyNowIdle = bulkError.busy.filter(key => {
    const slot = localSlots.find(s => s.key === key)
    return !!slot && !slot.running && partitionBulkSwitch([slot], bulkModelPick, bulkSkipRunning, bulkEffortPick).affected === 1
  })
  const bulkOutcomeNotices = bulkOutcomeShown
    ? [
        bulkSwitchedNow.length ? i18nT('pages.chatSidebar.bulk_switch_switched', { count: bulkSwitchedNow.length, names: bulkSessionNames(bulkSwitchedNow) }) : '',
        bulkError.busy.length ? i18nT('pages.chatSidebar.bulk_switch_skipped_busy', { count: bulkError.busy.length, names: bulkSessionNames(bulkError.busy) }) : '',
        bulkBusyNowIdle.length ? i18nT('pages.chatSidebar.bulk_switch_busy_now_idle', { count: bulkBusyNowIdle.length, names: bulkSessionNames(bulkBusyNowIdle) }) : '',
        bulkError.deferred.length ? i18nT('pages.chatSidebar.bulk_switch_effort_deferred', { count: bulkError.deferred.length, names: bulkSessionNames(bulkError.deferred) }) : '',
      ].filter(Boolean)
    : []
  // The sessions the Switch count leaves out because they are already on the
  // pick: on the model, and on the effort too while one is picked. Hidden while
  // an outcome is shown: the slots push moves the switched sessions onto the
  // pick, so they would read as already there and as switched at once.
  const bulkOnTargetNotice = !bulkModelPick || !bulkPartition.onTarget || bulkOutcomeShown ? ''
    : bulkEffortPick === undefined
      ? i18nT('pages.chatSidebar.already_on_model', { count: bulkPartition.onTarget, names: bulkSessionNames(bulkPartition.onTargetKeys) })
      : i18nT('pages.chatSidebar.already_on_model_and_effort', { count: bulkPartition.onTarget, names: bulkSessionNames(bulkPartition.onTargetKeys) })
  // Running sessions the switch could still touch. Before a pick every running
  // session counts; after one, a running session already on the pick is in the
  // "already use this model" line instead, so it is not counted twice.
  const bulkRunningCount = useMemo(
    () => (bulkModelPick ? bulkPartition.runningOffTarget : localSlots.filter(s => s.running).length),
    [bulkModelPick, bulkPartition, localSlots],
  )
  // The running sessions a switch that does not skip them stops mid-reply. An
  // effort-only change keeps the reply and takes effect at the next start.
  const bulkRunningStoppedCount = bulkModelPick ? bulkPartition.runningModelChange : bulkRunningCount
  // Back to a fresh panel: no model, no effort, no notice.
  const resetBulkPanel = () => { setBulkModel(''); setBulkEffort(BULK_EFFORT_KEEP); setBulkModelError('') }
  const bulkModelMutation = useMutation({
    // 'auto' goes on the wire verbatim (not collapsed to ''): '' doubles as the
    // "never chosen" state that every reader re-resolves to the agent template's
    // model, so it cannot express an explicit Auto pick.
    mutationFn: ({ model, skipRunning, effort }: { model: string; skipRunning: boolean; effort?: string }) =>
      api.chatSlotsModel(model, skipRunning, effort),
    onSuccess: (res, { model, effort }) => {
      // The switched models refresh on the next authoritative sseSlots push;
      // this handler does not eagerly reflect them. The previous dead-key
      // `invalidateQueries(['chat-slots'])` was a no-op (no query is registered
      // on that key; slot.model lives in the Redux dashboard slice), and an
      // eager client-side patch here would need a per-field reconciliation
      // contract to avoid overwriting a reordered authoritative frame -- that
      // belongs to the whole-list applySlots reducer-contract work in #11149,
      // not this rename-recovery fix. Removing the no-op keeps the pre-existing
      // behaviour without carrying that contract into this PR.
      // Partial outcome: the endpoint returns 200 with a non-empty `failed`
      // list when some slots' resets raised, `skipped_running` for the busy
      // ones it left alone, and `effort_deferred` for the ones that run the new
      // effort only from their next start. Any of them keeps the panel open,
      // naming its sessions, instead of reading as a switch that fully applied.
      const outcome = {
        switched: res.switched ?? [],
        busy: res.skipped_running ?? [],
        deferred: res.effort_deferred ?? [],
        failed: res.failed ?? [],
      }
      if (outcome.failed.length || outcome.busy.length || outcome.deferred.length) {
        setBulkError({ message: '', model, effort, ...outcome })
      } else {
        setBulkModelOpen(false)
        resetBulkPanel()
      }
    },
    onError: (e) => setBulkModelError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.switch_failed')),
  })
  // Roving-focus keyboard nav for the model list (WAI-ARIA listbox). No filter
  // input here, so the hook moves focus into the list on open; Escape/Tab close.
  const bulkListRef = useRef<HTMLDivElement>(null)
  const bulkInputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown: bulkOnListKeyDown } = useListboxKeyboard({
    open: bulkModelOpen,
    dropdownRef: bulkListRef,
    inputRef: bulkInputRef,
    hasFilterInput: false,
    filteredCount: bulkModelOptions.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: () => { setBulkModelOpen(false); resetBulkPanel() },
  })
  return { bulkModelOpen, setBulkModelOpen, setBulkModel, bulkSkipRunning, setBulkSkipRunning, bulkModelError, setBulkModelError, bulkSkipRunningLabelId, bulkModelsQuery, bulkModelOptions, bulkModelsFailed, bulkModelPick, bulkAffectedCount, bulkFailedCount: bulkError.failed.length, bulkRetriesFailed, bulkRunningCount, bulkRunningStoppedCount, bulkModelMutation, bulkListRef, bulkOnListKeyDown, bulkEffort, setBulkEffort, bulkEffortSupported, bulkEffortPick, bulkOutcomeApplied, bulkOutcomeNotices, bulkOnTargetNotice, resetBulkPanel }
}

/** The model switch panel. */
export function renderBulkModelPanel(shell: SidebarScope): React.ReactNode {
  const { bulkAffectedCount, bulkEffort, bulkEffortPick, bulkEffortSupported, bulkFailedCount,
    bulkListRef, bulkModelError, bulkModelMutation, bulkModelOptions, bulkModelPick, bulkModelsFailed, bulkModelsQuery,
    bulkOnListKeyDown, bulkOnTargetNotice, bulkOutcomeApplied, bulkOutcomeNotices, bulkRetriesFailed, bulkRunningCount, bulkRunningStoppedCount, bulkSkipRunning,
    bulkSkipRunningLabelId, resetBulkPanel, setBulkEffort, setBulkModel, setBulkModelError, setBulkModelOpen, setBulkSkipRunning } = shell.bulkModel
  const effortHintId = `${bulkSkipRunningLabelId}-effort-hint`
  return (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.switch_all_sessions')}</div>
          <div className="text-muted text-[12px] mb-2">
            {i18nT('pages.chatSidebar.bulk_switch_keeps_conversation')}
            {bulkRunningStoppedCount > 0 && !bulkSkipRunning && <> <span data-testid="bulk-model-running-caveat" className="text-danger">{i18nT('pages.chatSidebar.bulk_switch_stops_running_reply')}</span></>}
          </div>
          {bulkModelsFailed && (
            <div className="flex flex-wrap items-center gap-2 mb-2">
              {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
                  and the navigation would discard it. Retry stays in the panel.
                  Its own row above the listbox, wrapping the Retry button under
                  the notice when the sidebar is too narrow for both: an inline
                  notice sharing a fixed row collapses to one character per line
                  at sidebar width, and the Cancel/Switch row below is already at
                  the two-button limit. */}
              <ErrorNotice
                className="flex-1 min-w-[12rem]"
                message={i18nT('pages.chatSidebar.model_list_failed')}
                testId="bulk-model-roster-error"
              />
              {/* Busy through aria-disabled and the click guard, never native
                  `disabled`: disabling the button the reader just pressed blurs
                  it to <body>, and the notice stays up while the refetch runs. */}
              <Btn
                className="text-[12px] px-3 py-1 shrink-0 aria-disabled:opacity-30 aria-disabled:cursor-not-allowed"
                aria-disabled={bulkModelsQuery.isFetching || undefined}
                aria-busy={bulkModelsQuery.isFetching || undefined}
                onClick={() => { if (!bulkModelsQuery.isFetching) void bulkModelsQuery.refetch() }}
              >{i18nT('pages.chatSidebar.retry')}</Btn>
            </div>
          )}
          <div ref={bulkListRef} role="listbox" aria-label={i18nT('pages.chatSidebar.model_list')} tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-hidden">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModelPick} onSelect={setBulkModel} />
          </div>
          {/* Effort for the same switch. "Keep current effort" sends no level, so
              each session keeps its own. Disabled, with a dash and one line
              saying why, until a model that uses effort is picked. */}
          <div className="flex items-center gap-2 text-[12px] mb-2">
            <span className="text-muted shrink-0" aria-hidden="true">{i18nT('components.reasoningEffortDropdown.effort')}</span>
            <SimpleSelect
              value={bulkEffortSupported ? bulkEffort : BULK_EFFORT_NONE}
              triggerFallback="—"
              onChange={setBulkEffort}
              options={[BULK_EFFORT_KEEP, ...BULK_EFFORT_LEVELS]}
              optionLabels={[i18nT('pages.chatSidebar.bulk_effort_keep'), ...BULK_EFFORT_LEVELS.map(level => effortLabel(level))]}
              aria-label={i18nT('components.reasoningEffortDropdown.effort')}
              aria-describedby={bulkEffortSupported ? undefined : effortHintId}
              disabled={!bulkEffortSupported}
              style={{ flex: '1 1 0%', minWidth: 0 }}
            />
          </div>
          {!bulkEffortSupported && (
            <div id={effortHintId} data-testid="bulk-effort-unsupported" className="text-muted text-[11px] -mt-1 mb-2">{i18nT('pages.chatSidebar.bulk_effort_needs_effort_model')}</div>
          )}
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              {/* aria-labelledby, not aria-label: the name is the visible
                  "Skip N running sessions" text, one catalog key with a live
                  count. Binding it by reference keeps the announced name and
                  the rendered name the same string, so the count cannot drift. */}
              <input type="checkbox" aria-labelledby={bulkSkipRunningLabelId} checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              <span id={bulkSkipRunningLabelId}>{i18nT('pages.chatSidebar.skip_running_sessions', { count: bulkRunningCount, n: fmtNumber(bulkRunningCount) })}</span>
            </label>
          )}
          {/* The bucket the Switch count leaves out with no other surface:
              sessions already on the pick. Without it a lower count, or a
              disabled "Switch 0 sessions", cannot be reconciled with the rows. */}
          {bulkOnTargetNotice && (
            <div data-testid="bulk-model-on-target" role="status" className="text-[12px] text-muted mb-2 max-h-32 overflow-y-auto">
              {bulkOnTargetNotice}
            </div>
          )}
          {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
              and the navigation would discard it. Its own line, above the
              Cancel/Switch pair: a third control in that row would break
              max-two-buttons-per-row, and an inline notice sharing the row
              collapses to one character per line at sidebar width. */}
          <ErrorNotice message={bulkModelError} className="mb-2 max-h-32" scrollMessage testId="bulk-model-error" />
          {bulkOutcomeNotices.length > 0 && (
            <div data-testid="bulk-model-outcome" role="status" className="text-[12px] text-muted mb-2 max-h-48 overflow-y-auto">
              {bulkOutcomeNotices.map(line => <div key={line}>{line}</div>)}
            </div>
          )}
          <div className="flex items-center gap-2 justify-end">
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); resetBulkPanel() }}>{bulkOutcomeApplied ? i18nT('pages.chatSidebar.close') : i18nT('pages.chatSidebar.cancel')}</Btn>
            {/* While the switch is out, Switch is inert through aria-disabled and
                the guard, for the same reason as Retry above: a refused switch
                keeps the panel open, so focus must still be on this button. Native
                `disabled` waits for the mutation to settle: the slots push that
                lands mid-request can drop the count to 0 before the response. */}
            <Btn primary className="text-[12px] px-3 py-1 aria-disabled:bg-bg-hover aria-disabled:text-muted aria-disabled:border-transparent aria-disabled:shadow-none aria-disabled:opacity-60 aria-disabled:cursor-not-allowed aria-disabled:hover:bg-bg-hover aria-disabled:hover:shadow-none" disabled={!bulkModelMutation.isPending && (!bulkModelPick || (bulkAffectedCount === 0 && !bulkRetriesFailed))} aria-disabled={bulkModelMutation.isPending || undefined} aria-busy={bulkModelMutation.isPending || undefined} onClick={() => { if (bulkModelMutation.isPending) return; setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModelPick, skipRunning: bulkSkipRunning, effort: bulkEffortPick }) }}>{bulkModelMutation.isPending ? i18nT('pages.chatSidebar.switching') : bulkRetriesFailed ? i18nT('pages.chatSidebar.retry_failed_session', { count: bulkFailedCount }) : i18nT('pages.chatSidebar.switch_session', { count: bulkAffectedCount })}</Btn>
          </div>
        </div>
  )
}
