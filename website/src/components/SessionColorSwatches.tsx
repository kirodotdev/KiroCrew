import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { store, useAppDispatch } from '../store'
import { sseSlotColor } from '../store/dashboardSlice'
import { api } from '../api/client'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { colorName } from '../utils/sessionColors'

import { i18nT } from '../i18n/t'

/** Mirrors the backend contract in chat_persistence.COLOR_HEX_RE. */
const HEX_RE = /^#[0-9a-fA-F]{6}$/

/**
 * The inline session-colour swatch row used as the `colorSlot` of
 * SessionActionsMenu — shared by the session-header dropdown and the sidebar row
 * menus (dropdown + right-click). It is NOT a Radix menu item (a single
 * horizontal row of swatch buttons, not a focusable list), so key events are
 * stopped to avoid tripping Radix's typeahead/auto-close.
 *
 * Two write paths, both `useMutation` with optimistic `sseSlotColor` and a
 * guarded rollback (cf. `pinMutation` in useSessionActions):
 * - palette swatch → `color_index` (backend clears any custom hex);
 * - custom cell → `color_hex` via the native colour input + hex field
 *   (backend clears the index). The "no colour" cell clears both fields in
 *   one PATCH — an index-only null would leave a custom hex in place, since
 *   the endpoint is `in body`-gated.
 *
 * `onPicked` lets a caller that controls its own menu close it after a pick (the
 * header passes `setOpen(false)`); the sidebar menus are uncontrolled, so they
 * omit it and stay open — letting you try colours. Custom-hex commits never
 * call `onPicked`: the OS colour panel outlives the menu, and closing the row
 * mid-drag would orphan it.
 */
export default function SessionColorSwatches({ slotKey, colorIndex, colorHex, onPicked }: {
  slotKey: string
  colorIndex?: number | null
  colorHex?: string | null
  onPicked?: () => void
}) {
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()
  const [customOpen, setCustomOpen] = useState(false)
  const [draft, setDraft] = useState(colorHex || '#4f8ef7')
  const lastValidRef = useRef(HEX_RE.test(draft) ? draft : '#4f8ef7')
  const commitTimerRef = useRef<ReturnType<typeof setTimeout>>()
  // Blur only commits a draft the user actually edited: without this, merely
  // focusing the hex field and clicking away would silently paint the session
  // the seeded placeholder color.
  const dirtyRef = useRef(false)
  // Rollback identity. The value guards below cannot tell two in-flight writes
  // that target the SAME value apart, so a late failure of the first would roll
  // back over the second's success. Only the latest write may roll back.
  const writeGenRef = useRef(0)
  if (HEX_RE.test(draft)) lastValidRef.current = draft

  const readSlot = () => store.getState().dashboard.slots.find(s => s.key === slotKey)

  const colorMutation = useMutation({
    // idx === null is the "no colour" cell: clear BOTH fields in one PATCH.
    mutationFn: (idx: number | null) =>
      idx === null ? api.clearSlotColor(slotKey) : api.setSlotColor(slotKey, idx),
    onMutate: (idx) => {
      const s = readSlot()
      const prev = { color_index: s?.color_index ?? null, color_hex: s?.color_hex ?? null }
      const gen = ++writeGenRef.current
      dispatch(idx === null
        ? sseSlotColor({ key: slotKey, color_index: null, color_hex: null })
        : sseSlotColor({ key: slotKey, color_index: idx }))
      return { prev, gen }
    },
    onError: (_err, idx, ctx) => {
      if (!ctx) return
      // A superseded write never rolls back: a later pick (even one targeting
      // the same value, which the checks below cannot distinguish) owns the
      // state now.
      if (ctx.gen !== writeGenRef.current) return
      // Guarded rollback: only revert if the store still shows the value this
      // pick set — a superseding pick (rapid clicks) must not be clobbered
      // (same guard as useMoveSlotToFolder).
      const s = readSlot()
      if (idx === null) {
        // Clear rollback: a later custom-hex pick also leaves color_index
        // null, so checking the index alone would clobber it — require BOTH
        // fields to still be null (i.e. the clear is still the latest state).
        if ((s?.color_index ?? null) === null && (s?.color_hex ?? null) === null) {
          dispatch(sseSlotColor({ key: slotKey, ...ctx.prev }))
        }
        return
      }
      const current = s?.color_index ?? null
      if (current === idx) dispatch(sseSlotColor({ key: slotKey, ...ctx.prev }))
    },
  })

  const hexMutation = useMutation({
    mutationFn: (hex: string) => api.setSlotColorHex(slotKey, hex),
    onMutate: (hex) => {
      const s = readSlot()
      const prev = { color_index: s?.color_index ?? null, color_hex: s?.color_hex ?? null }
      const gen = ++writeGenRef.current
      dispatch(sseSlotColor({ key: slotKey, color_hex: hex }))
      return { prev, gen }
    },
    onError: (_err, hex, ctx) => {
      if (!ctx) return
      if (ctx.gen !== writeGenRef.current) return
      const current = readSlot()?.color_hex ?? null
      if (current === hex) dispatch(sseSlotColor({ key: slotKey, ...ctx.prev }))
    },
  })

  const pick = (idx: number | null) => {
    // An immediate selection supersedes any pending debounced wheel commit —
    // without this, a wheel drag followed within 300ms by a palette/no-color
    // click would let the delayed hex PATCH run last and persist stale color.
    if (commitTimerRef.current) clearTimeout(commitTimerRef.current)
    colorMutation.mutate(idx)
    onPicked?.()
  }

  const commitHex = (value: string) => {
    if (!HEX_RE.test(value)) return
    // A direct commit (Enter/blur) supersedes a pending wheel commit too.
    if (commitTimerRef.current) clearTimeout(commitTimerRef.current)
    dirtyRef.current = false
    hexMutation.mutate(value.toLowerCase())
  }

  /** Native colour input fires on every wheel movement — debounce the PATCH
   *  so a drag commits once, 300ms after the user settles. */
  const onWheelChange = (value: string) => {
    setDraft(value)
    if (commitTimerRef.current) clearTimeout(commitTimerRef.current)
    commitTimerRef.current = setTimeout(() => commitHex(value), 300)
  }

  return (
    <div onKeyDown={e => e.stopPropagation()}>
      <div className="flex items-center gap-1.5 px-3 py-1.5">
        <button type="button" aria-label={i18nT('components.sessionColorSwatches.no_color')} className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex == null && !colorHex ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => pick(null)} title={i18nT('components.sessionColorSwatches.no_color')} />
        {paletteColors.map((c, i) => (
          <button type="button" key={i} aria-label={colorName(c)} className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex === i ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: c }} onClick={() => pick(i)} title={colorName(c)} />
        ))}
        <button
          type="button"
          aria-label={i18nT('components.sessionColorSwatches.custom_color')}
          aria-expanded={customOpen}
          title={colorHex ? `${i18nT('components.sessionColorSwatches.custom_color')} (${colorHex})` : i18nT('components.sessionColorSwatches.custom_color')}
          className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorHex ? 'border-text-strong scale-110' : 'border-transparent'}`}
          // Always the multicolor wheel: it is the cell's identity as the
          // "pick any color" affordance. The active ring marks a set custom
          // color; the actual hex shows in the tooltip and on the row itself.
          style={{ background: 'conic-gradient(from 0deg, #f66 0deg, #fc6 60deg, #6d6 120deg, #6cc 180deg, #66f 240deg, #c6f 300deg, #f66 360deg)' }}
          onClick={() => setCustomOpen(o => !o)}
        />
      </div>
      {customOpen && (
        <div className="flex items-center gap-2 px-3 pb-1.5">
          <input
            type="color"
            value={lastValidRef.current}
            onChange={e => onWheelChange(e.target.value)}
            aria-label={i18nT('components.sessionColorSwatches.custom_color')}
            className="w-6 h-6 rounded cursor-pointer border border-border bg-transparent p-0"
          />
          <input
            type="text"
            value={draft}
            maxLength={7}
            spellCheck={false}
            onChange={e => {
              dirtyRef.current = true
              const v = e.target.value
              setDraft(v.startsWith('#') || v === '' ? v : `#${v}`)
            }}
            onKeyDown={e => {
              e.stopPropagation()
              if (e.key === 'Enter') { e.preventDefault(); commitHex(draft) }
            }}
            onBlur={() => { if (dirtyRef.current) commitHex(draft) }}
            aria-label={i18nT('components.sessionColorSwatches.hex_color_code')}
            placeholder="#4f8ef7"
            className="w-[76px] bg-bg-accent border border-border rounded px-1.5 py-0.5 text-[11px] font-mono text-text outline-none focus:border-accent"
          />
        </div>
      )}
    </div>
  )
}
