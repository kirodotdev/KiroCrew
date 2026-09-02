import { readableText, hexToRgba } from '../lib/format'

/** One label per row as a PILL: a light tint of the label's GitHub colour
 * when unselected, filling to the full colour when selected. Count shown
 * first; no colour dot (per product decision). */
export default function LabelRow({
  name, color, count, selected, title, onClick,
}: {
  name: string
  color: string
  count: number
  selected: boolean
  title?: string
  onClick: () => void
}) {
  const style: React.CSSProperties = selected
    ? { backgroundColor: `#${color}`, color: readableText(color) }
    : { backgroundColor: hexToRgba(color, 0.16), color: 'var(--text)' }
  return (
    <button
      onClick={onClick}
      title={title}
      style={style}
      className={`inline-flex items-center gap-2 max-w-full rounded-full px-3 py-1 text-[13px] cursor-pointer transition-colors ${selected ? 'font-bold' : 'font-medium'}`}
    >
      <span className={`tabular-nums text-[12px] flex-shrink-0 ${selected ? 'opacity-90' : 'opacity-60'}`}>
        {count}
      </span>
      <span className="truncate">{name}</span>
    </button>
  )
}
