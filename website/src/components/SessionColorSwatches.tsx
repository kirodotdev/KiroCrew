import { useMutation } from '@tanstack/react-query'
import { store, useAppDispatch } from '../store'
import { sseSlotColor } from '../store/dashboardSlice'
import { api } from '../api/client'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { colorName } from '../utils/sessionColors'

import { i18nT } from '../i18n/t'
/**
 * The inline session-colour swatch row used as the `colorSlot` of
 * SessionActionsMenu — shared by the session-header dropdown and the sidebar row
 * menus (dropdown + right-click). It is NOT a Radix menu item (a single
 * horizontal row of swatch buttons, not a focusable list), so key events are
 * stopped to avoid tripping Radix's typeahead/auto-close.
 *
 * The colour write goes through `useMutation` (the package's standard
 * server-write pattern, cf. `pinMutation` in useSessionActions): `onMutate`
 * applies the optimistic `sseSlotColor` and captures the prior index, `onError`
 * rolls back — so a failed `api.setSlotColor` does not silently strand the UI
 * on the wrong colour.
 *
 * `onPicked` lets a caller that controls its own menu close it after a pick (the
 * header passes `setOpen(false)`); the sidebar menus are uncontrolled, so they
 * omit it and stay open — letting you try colours.
 */
export default function SessionColorSwatches({ slotKey, colorIndex, onPicked }: {
  slotKey: string
  colorIndex?: number | null
  onPicked?: () => void
}) {
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()

  const colorMutation = useMutation({
    mutationFn: (idx: number | null) => api.setSlotColor(slotKey, idx),
    onMutate: (idx) => {
      const prev = store.getState().dashboard.slots.find(s => s.key === slotKey)?.color_index ?? null
      dispatch(sseSlotColor({ key: slotKey, color_index: idx }))
      return { prev }
    },
    onError: (_err, idx, ctx) => {
      if (!ctx) return
      // Guarded rollback: only revert if the store still shows the value this
      // pick set — a superseding pick (rapid clicks) must not be clobbered
      // (same guard as useMoveSlotToFolder).
      const current = store.getState().dashboard.slots.find(s => s.key === slotKey)?.color_index ?? null
      if (current === idx) dispatch(sseSlotColor({ key: slotKey, color_index: ctx.prev }))
    },
  })

  const pick = (idx: number | null) => {
    colorMutation.mutate(idx)
    onPicked?.()
  }

  return (
    <div className="flex items-center gap-1.5 px-3 py-1.5" onKeyDown={e => e.stopPropagation()}>
      <button type="button" aria-label={i18nT('components.sessionColorSwatches.no_color')} className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex == null ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => pick(null)} title={i18nT('components.sessionColorSwatches.no_color')} />
      {paletteColors.map((c, i) => (
        <button type="button" key={i} aria-label={colorName(c)} className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex === i ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: c }} onClick={() => pick(i)} title={colorName(c)} />
      ))}
    </div>
  )
}
