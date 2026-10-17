/** The crew window's model + effort picker: the local composer's own chip and
 *  ModelEffortDropdown, fed the PEER's roster and writing the PEER's slot.
 *
 *  The roster is the peer's (`/api/instances/{id}/capabilities`, already read
 *  by the window for its version gate) and the effort levels are the peer
 *  slot's own selection capabilities, so the menu never offers a model or a
 *  level this machine has and the peer does not. Both writes go through the
 *  hub proxy to the peer's own slot routes. */
import { useCallback, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import ModelEffortDropdown from '../../../components/ModelEffortDropdown'
import type { ModelItem } from '../../../components/ModelDropdownList'
import { useAnchoredTriggerRect } from '../../../hooks/useAnchoredTriggerRect'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../../../hooks/useListboxKeyboard'
import type { RemoteCrewCapabilities } from '../../../types'

/** The model fields of the peer's slot row. */
export interface PeerModelFields { model?: string; served_model?: string; reasoning_effort?: string }

interface PeerSelectionCaps { known?: boolean; effort_supported?: boolean; effort_levels?: string[] }

/** How often a not-yet-known capability answer is asked again. */
const CAPS_RETRY_MS = 5000

export function useCrewWindowModelPicker({ instanceId, slotKey, slotPath, slot, caps, enabled, onWritten }: {
  instanceId: string
  /** The window's own key (`crewWindowSlot`): it names no local session. */
  slotKey: string
  /** `api/chat/slots/<key>` on the peer. */
  slotPath: string
  slot: PeerModelFields | null | undefined
  caps: RemoteCrewCapabilities | undefined
  enabled: boolean
  /** A write landed (or failed): re-read the peer's slot. */
  onWritten: () => void
}) {
  const selectionQ = useQuery({
    queryKey: ['crew-window', instanceId, slotPath, 'selection-capabilities', slot?.model ?? ''],
    queryFn: () => api.crewPeerGet(instanceId, slotPath + '/selection-capabilities') as Promise<PeerSelectionCaps>,
    enabled,
    // A peer session that has not started yet answers `known: false`; ask
    // again until it knows, so the effort control appears once it does.
    // A failed read is asked again too, so the effort control comes back once
    // the peer answers instead of staying gone for the window's life.
    refetchInterval: q => (q.state.status === 'error' || q.state.data?.known === false ? CAPS_RETRY_MS : false),
  })
  const models = useMemo<ModelItem[]>(
    () => (caps?.models ?? []).map(m => ({ name: m.model_name, description: m.description })),
    [caps?.models],
  )
  const dd = useFilteredDropdown(models)
  const { rect, anchorTo } = useAnchoredTriggerRect(dd.open)
  const [error, setError] = useState<unknown>(null)
  const write = useCallback(async (path: string, body: object): Promise<unknown> => {
    setError(null)
    try {
      return await api.crewPeerPost(instanceId, slotPath + path, body)
    } catch (e) {
      setError(e)
      throw e
    } finally {
      onWritten()
    }
  }, [instanceId, slotPath, onWritten])
  const pickModel = useCallback((name: string) => {
    dd.setOpen(false)
    write('/model', { model: name }).catch(() => { /* shown through `error` */ })
  }, [dd, write])
  // The slider owns the effort write's outcome (refusal or confirmation
  // timeout) and hands any failure to `onWriteError`, which shows it on the
  // window's own notice: an embedded window draws no dashboard switch notice.
  const writeEffort = useCallback(
    (level: string) => (api.crewPeerPost(instanceId, slotPath + '/reasoning-effort', { reasoning_effort: level }) as Promise<{ reasoning_effort?: string; model?: string }>).finally(onWritten),
    [instanceId, slotPath, onWritten],
  )
  const onWriteError = useCallback((message: string) => setError(new Error(message)), [])
  const { onListKeyDown } = useListboxKeyboard({
    open: dd.open,
    dropdownRef: dd.dropdownRef,
    inputRef: dd.inputRef,
    hasFilterInput: true,
    filteredCount: dd.filtered.length,
    onEnterSingleMatch: () => pickModel(dd.filtered[0].name),
    closeToTrigger: () => dd.setOpen(false),
  })

  const hasEffort = selectionQ.data?.effort_supported === true
  const chipProps = {
    modelName: slot?.served_model || slot?.model || 'auto',
    modelIsInheritedDefault: !slot?.model && !!slot?.served_model,
    reasoningEffort: slot?.reasoning_effort || '',
    effortIsDefault: !slot?.reasoning_effort,
    hasEffort,
    onModelClick: (r: DOMRect, trigger?: HTMLElement) => { anchorTo(r, trigger); dd.setOpen(!dd.open) },
  }
  const portal = dd.open && rect ? createPortal(
    <ModelEffortDropdown
      anchorRect={rect}
      dropdownRef={dd.dropdownRef}
      inputRef={dd.inputRef}
      onListKeyDown={onListKeyDown}
      models={dd.filtered}
      activeModel={slot?.model || slot?.served_model || 'auto'}
      onSelectModel={pickModel}
      modelsFailed={!!caps?.unavailable?.models}
      filter={dd.filter}
      setFilter={dd.setFilter}
      onClose={() => dd.setOpen(false)}
      hasEffort={hasEffort}
      // Keys the slider's switch protocol only; the write goes to the peer.
      slot={slotKey}
      currentEffort={slot?.reasoning_effort || ''}
      effortLevelsOverride={selectionQ.data?.effort_levels ?? caps?.effort_levels ?? []}
      writeEffort={writeEffort}
      onWriteError={onWriteError}
    />,
    document.body,
  ) : null
  // A failed capability read hides the effort level, so say so (as a split
  // pane does) rather than let it look unsupported.
  return { chipProps, portal, error, clearError: () => setError(null), effortCapsFailed: selectionQ.isError }
}
