import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { DndContext, closestCenter, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { GripVertical, Pin, RotateCcw } from 'lucide-react'

import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { api } from '../api/client'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { applyModelOrder, mergeReorderedNames } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'
import { useProvider } from '../providers'
import { useModelsDegraded } from '../providers/modelListHealth'
import { useDndSensors } from '../hooks/useDndSensors'
import { useOptimisticConfigPaths, setConfigPathValue } from '../pages/settings/useOptimisticConfigPaths'
import { i18nT } from '../i18n/t'

/** Config path this card owns. A string list; the backend validates it as
 *  `str_list` (see handlers/core.py) and `[]` means "backend order". */
const MODEL_ORDER_PATH = 'agent.model_order'

/** The one field of the kirocrewConfig payload this card reads. ChatPanel's own
 *  KirocrewConfigShape is not exported, and the config PATCH merges by path, so
 *  a narrow local shape is enough and cannot clobber a sibling field. */
type ModelOrderConfig = { agent?: { model_order?: string[] } }

/**
 * Order live model NAMES by the saved order — a projection through
 * `applyModelOrder` (providers/modelList.ts, the sibling half of this feature),
 * so ONE function owns the ordering rule for both the pickers and this drag
 * surface. Stripping `auto` afterwards is deliberate: it is the pinned first
 * row, never reordered.
 *
 * The list is driven by this projection of the SAVED ORDER rather than by the
 * hook's own ordering so a drop shows instantly through the optimistic
 * overlay's `shown()` value — the hook re-derives from the ['kirocrewConfig']
 * cache, which only updates after the PATCH round-trips. When the hook also
 * applies the order the two agree (idempotent, applyModelOrder is), so this
 * stays correct whether or not the hook has been wired to reorder yet.
 */
export function orderModelNames(models: ModelInfo[], order: string[]): string[] {
  return applyModelOrder(models, order)
    .map(m => m.name)
    .filter(name => name !== 'auto')
}

/**
 * One draggable model row. Mirrors ChatSidebar's SortableFolderBlock and App's
 * SortableAppNavRow: the sortable transform positions the row so its siblings
 * reflow to open a gap as it is dragged. A dedicated grip button carries the
 * drag listeners AND the keyboard reordering — the KeyboardSensor
 * (useDndSensors keyboard:true) walks the focused handle through the sortable
 * ring — so the row's name text stays selectable and the affordance is one
 * labelled control rather than the whole row.
 */
function SortableModelRow({ name }: { name: string }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: name })
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
  }
  return (
    <li
      ref={setNodeRef}
      style={style}
      data-model-row={name}
      className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5"
    >
      <button
        type="button"
        // `touch-none` hands the touch gesture to the TouchSensor's press-and-hold
        // rather than letting it scroll; `cursor-grab` marks the handle.
        className="cursor-grab touch-none text-muted hover:text-text focus-visible:text-text bg-transparent border-none p-0 flex items-center"
        aria-label={i18nT('settings.chat.modelOrder.reorder', { model: name })}
        {...attributes}
        {...listeners}
      >
        <GripVertical size={14} />
      </button>
      <span className="text-[13px] text-text">{name}</span>
    </li>
  )
}

/** Auto's fixed, non-draggable first row — communicates the pin that
 *  `applyModelOrder` enforces (auto stays first regardless of the saved order). */
function AutoRow() {
  return (
    <li className="flex items-center gap-2 rounded-md border border-dashed border-border px-2.5 py-1.5">
      <Pin size={14} className="text-muted" />
      <span className="text-[13px] text-text">{i18nT('settings.chat.modelOrder.autoLabel')}</span>
      {/* A div, not a span, deliberately: the name and this badge are two
          SEPARATE catalog units positioned apart by the flex row — not one
          sentence assembled from two keys. The i18n render gate joins adjacent
          inline elements into one text run and flags multi-key runs as
          reorder-hostile fragments (render-scan.mjs gradeRun); a block element
          ends the run, which states the truth: these units reorder
          independently and translators translate them independently. */}
      <div className="ml-auto text-[11px] text-muted">{i18nT('settings.chat.modelOrder.autoPinned')}</div>
    </li>
  )
}

/**
 * Drag-to-reorder editor for the model list, hosted by ModelEffortDropdown's
 * "Model order" drill-in page (maintainer review on PR #9969 moved it here
 * from a Settings ▸ Chat card — same interaction model, relocated shell).
 * Persists the full order array to `agent.model_order` through the same
 * useOptimisticConfigPaths machinery the settings selectors use, so a drop
 * shows immediately and a failed write reports on this editor's own path
 * without touching the others.
 */
export function ModelOrderEditor() {
  const qc = useQueryClient()
  const overlay = useOptimisticConfigPaths(qc)
  const provider = useProvider()
  const [saveError, setSaveError] = useState('')

  // keyboard:true wires the sortable coordinate getter for arrow-key reordering;
  // the mouse activation distance lets a plain click on the grip through.
  const sensors = useDndSensors({ distance: 5, keyboard: true })

  const cfgQ = useQuery<ModelOrderConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const savedOrder = cfgQ.data?.agent?.model_order ?? []
  const shownOrder = overlay.shown<string[]>(MODEL_ORDER_PATH, savedOrder)

  const models = useAvailableModels()
  // The drag list is every live model except Auto (the pinned row above it).
  const liveNames = models.map(m => m.name).filter(name => name !== 'auto')
  // Degraded = the health flag, OR no real models yet (placeholder/loading),
  // OR the saved order itself not yet loaded. A degraded list is rendered
  // READ-ONLY so a drop can never persist a truncated order over the saved one
  // (design §5), and the config gate closes the sibling hazard: with cached
  // models but the ['kirocrewConfig'] read still pending (or failed),
  // `savedOrder` is the `[]` fallback, so an enabled drag or reset would
  // persist an order derived from nothing and overwrite the user's saved one.
  // `isSuccess` covers both: pending and error stay read-only, and a settled
  // query retains its data across background refetches so the gate never
  // re-closes after first load.
  const degraded = useModelsDegraded(provider.id) || liveNames.length === 0 || !cfgQ.isSuccess

  const displayNames = orderModelNames(models, shownOrder)

  const orderMut = useMutation(
    overlay.mutationOpts<string[]>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (order: string[]) => api.patchConfig(MODEL_ORDER_PATH, order),
      path: () => MODEL_ORDER_PATH,
      displayValue: order => order,
      applyToCache: (cached, order) => setConfigPathValue(cached as ModelOrderConfig, MODEL_ORDER_PATH, order),
      onFailure: () => setSaveError(i18nT('settings.chat.modelOrder.saveFailed')),
      // A fresh drop supersedes this card's stale failure banner.
      onSupersede: () => setSaveError(''),
    })
  )

  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = displayNames.indexOf(String(active.id))
    const to = displayNames.indexOf(String(over.id))
    if (from === -1 || to === -1) return
    // The drag list shows only LIVE models (applyModelOrder skips stale saved
    // ids so they never render as phantom rows) — so persisting the rendered
    // array verbatim would silently delete every stale id on the first drag.
    // mergeReorderedNames writes the reordered live sequence back INTO the
    // saved order, keeping each stale id in its saved slot (the write-side
    // contract: an unknown-but-well-formed id is kept, because kiro renames
    // models and the order must outlive a rename). Every live model still
    // gets an explicit slot, so applyModelOrder has no "unlisted" tail to
    // append and the order is exact.
    orderMut.mutate(mergeReorderedNames(shownOrder, arrayMove(displayNames, from, to)))
  }

  // While degraded, show the saved order (or whatever cached models remain),
  // read-only. Never the empty drag list, and never a control that could write.
  const readOnlyNames = liveNames.length > 0 ? displayNames : shownOrder

  return (
    <div
      data-setting-key={MODEL_ORDER_PATH}
      className="flex flex-col gap-1.5 px-1.5 py-1 overflow-y-auto max-h-[280px]"
    >
      <div className="text-[12px] text-muted">{i18nT('settings.chat.modelOrder.description')}</div>

        {/* askAgent on: a failed order save persists nothing client-side — the
            drag list still shows the attempted order and another drop retries,
            so no unsaved draft is at risk of being abandoned to the agent. */}
        <ErrorNotice variant="inline" message={saveError || null} onDismiss={() => setSaveError('')} askAgent />

        {/* A failed ['kirocrewConfig'] READ is surfaced here, on the surface
            that owns the order, rather than in useAvailableModels: the hook
            has no render surface, and the pickers deliberately fall back to
            the backend order (documented at the hook's ordering seam) —
            silently for them, but never silently on this card, which would
            otherwise show "backend order" as if it were the saved state.
            askAgent on: nothing editable is at risk — the card is read-only
            while the read is failing (the `degraded` gate), and React Query
            retries the read on its own. No dismiss: the state clears itself
            when the read succeeds, and dismissing it manually would leave a
            read-only card with no explanation. */}
        <ErrorNotice variant="inline" message={cfgQ.isError ? i18nT('settings.chat.modelOrder.loadFailed') : null} askAgent />

        {degraded ? (
          <>
            <ul className="flex flex-col gap-1.5 mt-1">
              <AutoRow />
              {readOnlyNames.map(name => (
                <li
                  key={name}
                  className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 opacity-70"
                >
                  <span className="text-[13px] text-text">{name}</span>
                </li>
              ))}
            </ul>
            <div className="text-[12px] text-muted">{i18nT('settings.chat.modelOrder.degraded')}</div>
          </>
        ) : (
          <>
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
              <ul className="flex flex-col gap-1.5 mt-1">
                <AutoRow />
                <SortableContext items={displayNames} strategy={verticalListSortingStrategy}>
                  {displayNames.map(name => (
                    <SortableModelRow key={name} name={name} />
                  ))}
                </SortableContext>
              </ul>
            </DndContext>
            <div className="mt-1">
              <Btn onClick={() => orderMut.mutate([])} className="text-[12px]">
                <RotateCcw size={13} />
                {i18nT('settings.chat.modelOrder.reset')}
              </Btn>
            </div>
          </>
        )}
    </div>
  )
}
