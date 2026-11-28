/** The crew window's model + effort picker: the local composer's own chip and
 *  ModelEffortDropdown, fed the PEER's roster and writing the PEER's slot.
 *
 *  The roster is the peer's (`/api/instances/{id}/capabilities`, already read
 *  by the window for its version gate) and the effort levels are the peer
 *  slot's own selection capabilities, so the menu never offers a model or a
 *  level this machine has and the peer does not. Both writes go through the
 *  hub proxy to the peer's own slot routes. */
import { useCallback, useMemo, useState } from 'react'
import { OverlayPortal } from '../../../components/overlayRoot'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { retryThroughRestart } from '../../../api/queryClient'
import { effortInForce, effortOffOnAuto, selectionCapabilitiesFailed } from '../../../lib/effort'
import ModelEffortDropdown from '../../../components/ModelEffortDropdown'
import type { ModelItem } from '../../../components/ModelDropdownList'
import { useAnchoredTriggerRect } from '../../../hooks/useAnchoredTriggerRect'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../../../hooks/useListboxKeyboard'
import type { RemoteCrewCapabilities } from '../../../types'

/** The model fields of the peer's slot row. */
export interface PeerModelFields { model?: string; served_model?: string; reasoning_effort?: string }

interface PeerSelectionCaps { known?: boolean; effort_supported?: boolean; effort_levels?: string[]; effort_unapplied?: string; effort_running?: string; effort_inherited?: boolean }

/** How often an unknown or failed capability answer is asked again. */
const CAPS_RETRY_MS = 5000
/** An inherited default changed elsewhere must reach the chip. */
const CAPS_REFRESH_MS = 30_000

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
    queryKey: ['crew-window', instanceId, slotPath, 'selection-capabilities', slot?.model ?? '', slot?.reasoning_effort ?? ''],
    queryFn: () => api.crewPeerGet(instanceId, slotPath + '/selection-capabilities') as Promise<PeerSelectionCaps>,
    enabled,
    // An effort pick re-keys this read, and a key with no answer would hide the
    // slider while it is in use. Keep the previous answer while the new one
    // loads, but only for the same peer, slot and model: another model's
    // levels are not this one's. Drop its running/unapplied pair, which
    // belongs to the previous level: after a cleared pick it would name the
    // old level as running. Inline, so react-query re-asks on every key change
    // instead of carrying a placeholder across a later model change.
    placeholderData: (prev, prevQuery) => {
      const k = prevQuery?.queryKey
      return prev && k?.[1] === instanceId && k[2] === slotPath && k[4] === (slot?.model ?? '')
        ? { ...prev, effort_unapplied: undefined, effort_running: undefined }
        : undefined
    },
    // A failure shows the "could not verify" notice, so a restart or a dropped
    // link is ridden out first (see `retryThroughRestart`).
    retry: retryThroughRestart,
    // A peer session that has not started yet answers `known: false`; ask
    // again until it knows, so the effort control appears once it does.
    // A failed read uses the same fast retry. Once an answer is known, refresh
    // less often so a changed inherited default reaches the chip.
    refetchInterval: q => {
      if (q.state.status === 'error' || q.state.data?.known === false) return CAPS_RETRY_MS
      return q.state.data?.known === true ? CAPS_REFRESH_MS : false
    },
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
  const shownModel = slot?.served_model || slot?.model || 'auto'
  // No effort control on `auto`: the picker says why, as the pane and composer
  // pickers do (#11546). The peer advertises effort levels only when its
  // provider takes effort at all.
  const effortAutoNote = effortOffOnAuto(
    (caps?.effort_levels?.length ?? 0) > 0,
    hasEffort,
    selectionCapabilitiesFailed(selectionQ),
    shownModel,
  )
  // What runs, when the peer's model does not offer the slot's level.
  const effort = effortInForce(slot?.reasoning_effort || '', selectionQ.data, !!slot?.reasoning_effort)
  const chipProps = {
    modelName: shownModel,
    modelIsInheritedDefault: !slot?.model && !!slot?.served_model,
    reasoningEffort: effort.level,
    effortNotOffered: effort.notOffered,
    effortIsDefault: !slot?.reasoning_effort,
    hasEffort,
    onModelClick: (r: DOMRect, trigger?: HTMLElement) => { anchorTo(r, trigger); dd.setOpen(!dd.open) },
  }
  const portal = dd.open && rect ? <OverlayPortal>
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
      effortOffOnAuto={effortAutoNote}
      // Keys the slider's switch protocol only; the write goes to the peer.
      slot={slotKey}
      currentEffort={slot?.reasoning_effort || ''}
      effortLevelsOverride={selectionQ.data?.effort_levels ?? caps?.effort_levels ?? []}
      writeEffort={writeEffort}
      onWriteError={onWriteError}
    />
  </OverlayPortal> : null
  // A failed capability read hides the effort level, so say so (as a split
  // pane does) rather than let it look unsupported. A 404 is a slot the peer has
  // not registered yet, not a failure (`selectionCapabilitiesFailed`).
  return { chipProps, portal, error, clearError: () => setError(null), effortCapsFailed: selectionCapabilitiesFailed(selectionQ) }
}
