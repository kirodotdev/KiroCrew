import { useEffect, useRef, useState } from 'react'
import { useAppDispatch } from '../../store'
import { sseSlotColor } from '../../store/dashboardSlice'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { colorName } from '../../utils/sessionColors'
import { api } from '../../api/client'
import { Popover, PopoverTrigger, PopoverContent } from '../../components/ui/popover'
import ErrorNotice from '../../components/ErrorNotice'

import { i18nT } from '../../i18n/t'
export default function SessionColorPicker({ slotKey, colorIndex }: { slotKey?: string; colorIndex?: number | null }) {
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()
  const [open, setOpen] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // Generation counter for in-flight saves. Two rapid picks race: if the first
  // request fails AFTER the second succeeded, a naive rollback would overwrite
  // the newer, persisted colour. Only the latest write may close the popover,
  // roll back, or report — a stale settlement is ignored.
  const writeGen = useRef(0)
  // The colour the server is known to hold, so a rollback restores the last
  // CONFIRMED value rather than whatever optimistic colour preceded the click.
  // Seeded from the prop (what the server sent) and advanced only on success.
  const confirmed = useRef<number | null>(colorIndex ?? null)
  useEffect(() => {
    // A colour change from elsewhere (another tab, a server push) is also
    // confirmed server state — but not while a write of ours is in flight,
    // since the prop then reflects our own optimistic dispatch.
    if (writeGen.current === 0) confirmed.current = colorIndex ?? null
  }, [colorIndex])

  const color = colorIndex != null && colorIndex >= 0 && colorIndex < paletteColors.length ? paletteColors[colorIndex] : null

  if (!slotKey) return null

  const pick = (idx: number | null) => {
    // Optimistic: the dot recolours at once and the popover closes once the
    // save lands. A refused save used to be swallowed, so the dot kept a colour
    // the server never stored and the next reload silently reverted it. Roll
    // the store back to what the server still holds and keep the popover open
    // with the reason, so the person can pick again or hand it to the agent.
    const gen = ++writeGen.current
    setSaveError(null)
    dispatch(sseSlotColor({ key: slotKey, color_index: idx }))
    api.setSlotColor(slotKey, idx)
      .then(() => {
        if (gen !== writeGen.current) return
        writeGen.current = 0
        confirmed.current = idx
        setOpen(false)
      })
      .catch(() => {
        if (gen !== writeGen.current) return
        writeGen.current = 0
        dispatch(sseSlotColor({ key: slotKey, color_index: confirmed.current }))
        setSaveError(i18nT('pages.chat.sessionColorPicker.save_failed'))
      })
  }

  return (
    <Popover open={open} onOpenChange={o => { setOpen(o); if (!o) setSaveError(null) }}>
      <PopoverTrigger asChild>
        <button className="shrink-0 cursor-pointer transition-all hover:scale-125 pl-1" title={i18nT('pages.chat.sessionColorPicker.session_color')} aria-label={i18nT('pages.chat.sessionColorPicker.session_color')}>
          <span className="block w-3 h-3 rounded-full border-[1.5px] transition-colors" style={color ? { background: color, borderColor: color, boxShadow: `0 0 4px ${color}` } : { background: 'transparent', borderColor: 'var(--muted)' }} />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="p-2.5 w-fit">
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={i18nT('pages.chat.sessionColorPicker.session_colors')}>
          <button type="button" aria-label={i18nT('pages.chat.sessionColorPicker.no_color')} aria-pressed={colorIndex == null} className={`w-6 h-6 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${colorIndex == null ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => pick(null)} title={i18nT('pages.chat.sessionColorPicker.no_color')} />
          {paletteColors.map((c, i) => (
            <button type="button" key={i} aria-label={colorName(c)} aria-pressed={colorIndex === i} className={`w-6 h-6 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${colorIndex === i ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: c }} onClick={() => pick(i)} title={colorName(c)} />
          ))}
        </div>
        <div className="text-[11px] text-muted mt-1.5">{i18nT('pages.chat.sessionColorPicker.change_your_color_palette_in_display_settings')}</div>
        {/* askAgent on: the picker holds no draft — the store is already rolled
            back to the colour the server kept, so a hand-off loses nothing. */}
        <ErrorNotice variant="inline" message={saveError} askAgent className="mt-1.5 max-w-[240px]" onDismiss={() => setSaveError(null)} />
      </PopoverContent>
    </Popover>
  )
}
