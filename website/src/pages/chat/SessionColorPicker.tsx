import { useState } from 'react'
import { useAppDispatch } from '../../store'
import { sseSlotColor } from '../../store/dashboardSlice'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { colorName } from '../../utils/sessionColors'
import { api } from '../../api/client'
import { Popover, PopoverTrigger, PopoverContent } from '../../components/ui/popover'

import { i18nT } from '../../i18n/t'
export default function SessionColorPicker({ slotKey, colorIndex }: { slotKey?: string; colorIndex?: number | null }) {
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()
  const [open, setOpen] = useState(false)

  const color = colorIndex != null && colorIndex >= 0 && colorIndex < paletteColors.length ? paletteColors[colorIndex] : null

  if (!slotKey) return null

  const pick = (idx: number | null) => {
    dispatch(sseSlotColor({ key: slotKey, color_index: idx }))
    api.setSlotColor(slotKey, idx).catch(() => {})
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
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
      </PopoverContent>
    </Popover>
  )
}
