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
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { useListboxKeyboard } from '../../hooks/useListboxKeyboard'
import { partitionBulkSwitch } from '../../lib/bulkModelSwitch'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import type { SidebarScope } from './scope'

/** The model switch panel's state, roster and write. */
export function useBulkModelSwitch({ localSlots }: { localSlots: Slot[] }) {
  // Bulk model switch — apply one model to every live session at once.
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkModelError, setBulkModelError] = useState('')
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
  // Count only slots that would actually change. Every slot left out lands in
  // a named bucket the panel renders (see partitionBulkSwitch), so the
  // "Switch N" label can be reconciled with the rows on screen.
  const bulkPartition = useMemo(
    () => partitionBulkSwitch(localSlots, bulkModelPick, bulkSkipRunning),
    [localSlots, bulkModelPick, bulkSkipRunning],
  )
  const bulkAffectedCount = bulkPartition.affected
  // Running sessions the switch could still touch. Before a pick every running
  // session counts; after one, a running session already on the pick is in the
  // "already use this model" line instead, so it is not counted twice.
  const bulkRunningCount = useMemo(
    () => (bulkModelPick ? bulkPartition.runningOffTarget : localSlots.filter(s => s.running).length),
    [bulkModelPick, bulkPartition, localSlots],
  )
  const bulkModelMutation = useMutation({
    // 'auto' goes on the wire verbatim (not collapsed to ''): '' doubles as the
    // "never chosen" state that every reader re-resolves to the agent template's
    // model, so it cannot express an explicit Auto pick.
    mutationFn: ({ model, skipRunning }: { model: string; skipRunning: boolean }) =>
      api.chatSlotsModel(model, skipRunning),
    onSuccess: (res) => {
      // The switched models refresh on the next authoritative sseSlots push;
      // this handler does not eagerly reflect them. The previous dead-key
      // `invalidateQueries(['chat-slots'])` was a no-op (no query is registered
      // on that key; slot.model lives in the Redux dashboard slice), and an
      // eager client-side patch here would need a per-field reconciliation
      // contract to avoid overwriting a reordered authoritative frame -- that
      // belongs to the whole-list applySlots reducer-contract work in #11149,
      // not this rename-recovery fix. Removing the no-op keeps the pre-existing
      // behaviour without carrying that contract into this PR.
      // Partial failure: the endpoint returns 200 with a non-empty `failed`
      // list when some slots' resets raised. Surface it and keep the panel
      // open instead of silently closing on a partial success.
      if (res.failed?.length) {
        setBulkModelError(i18nT('pages.chatSidebar.session_failed_to_switch', { count: res.failed.length }))
      } else {
        setBulkModelOpen(false)
        setBulkModel('')
        setBulkModelError('')
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
    closeToTrigger: () => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') },
  })
  return { bulkModelOpen, setBulkModelOpen, setBulkModel, bulkSkipRunning, setBulkSkipRunning, bulkModelError, setBulkModelError, bulkSkipRunningLabelId, bulkModelsQuery, bulkModelOptions, bulkModelsFailed, bulkModelPick, bulkPartition, bulkAffectedCount, bulkRunningCount, bulkModelMutation, bulkListRef, bulkOnListKeyDown }
}

/** The model switch panel. */
export function renderBulkModelPanel(shell: SidebarScope): React.ReactNode {
  const { bulkAffectedCount, bulkListRef, bulkModelError, bulkModelMutation, bulkModelOptions, bulkModelPick,
    bulkModelsFailed, bulkModelsQuery, bulkOnListKeyDown, bulkPartition, bulkRunningCount, bulkSkipRunning,
    bulkSkipRunningLabelId, setBulkModel, setBulkModelError, setBulkModelOpen, setBulkSkipRunning } = shell.bulkModel
  return (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.switch_all_sessions')}</div>
          <div className="text-muted text-[12px] mb-2">
            {i18nT('pages.chatSidebar.bulk_switch_keeps_conversation')}
            {bulkRunningCount > 0 && !bulkSkipRunning && <> <span data-testid="bulk-model-running-caveat" className="text-danger">{i18nT('pages.chatSidebar.bulk_switch_stops_running_reply')}</span></>}
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
              <Btn
                className="text-[12px] px-3 py-1 shrink-0"
                disabled={bulkModelsQuery.isFetching}
                onClick={() => bulkModelsQuery.refetch()}
              >{i18nT('pages.chatSidebar.retry')}</Btn>
            </div>
          )}
          <div ref={bulkListRef} role="listbox" aria-label={i18nT('pages.chatSidebar.model_list')} tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-hidden">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModelPick} onSelect={setBulkModel} />
          </div>
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              {/* aria-labelledby, not aria-label: the name is the visible
                  "Skip N running sessions" text, which is two catalog keys plus a
                  live count. Binding it by reference keeps the announced name and
                  the rendered name the same string, so the count cannot drift. */}
              <input type="checkbox" aria-labelledby={bulkSkipRunningLabelId} checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              <span id={bulkSkipRunningLabelId}>{i18nT('pages.chatSidebar.skip')} {i18nT('pages.chatSidebar.running_session', { count: bulkRunningCount })}</span>
            </label>
          )}
          {/* The bucket the Switch count leaves out with no other surface:
              sessions already on the pick. Without it a lower count, or a
              disabled "Switch 0 sessions", cannot be reconciled with the rows. */}
          {bulkModelPick && bulkPartition.onTarget > 0 && (
            <div data-testid="bulk-model-on-target" role="status" className="text-[12px] text-muted mb-2">
              {i18nT('pages.chatSidebar.already_on_model', { count: bulkPartition.onTarget })}
            </div>
          )}
          {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
              and the navigation would discard it. Its own line, above the
              Cancel/Switch pair: a third control in that row would break
              max-two-buttons-per-row, and an inline notice sharing the row
              collapses to one character per line at sidebar width. */}
          <ErrorNotice message={bulkModelError} className="mb-2" testId="bulk-model-error" />
          <div className="flex items-center gap-2 justify-end">
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') }}>{i18nT('pages.chatSidebar.cancel')}</Btn>
            <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!bulkModelPick || bulkAffectedCount === 0 || bulkModelMutation.isPending} onClick={() => { setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModelPick, skipRunning: bulkSkipRunning }) }}>{bulkModelMutation.isPending ? i18nT('pages.chatSidebar.switching') : i18nT('pages.chatSidebar.switch_session', { count: bulkAffectedCount })}</Btn>
          </div>
        </div>
  )
}
