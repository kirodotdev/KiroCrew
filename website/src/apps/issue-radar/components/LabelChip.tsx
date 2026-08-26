import { readableText } from '../lib/format'

/** Solid display chip used in the issue list & detail (not interactive). */
export default function LabelChip({ name, color, small }: { name: string; color: string; small?: boolean }) {
  return (
    <span
      className={`inline-flex items-center rounded-full font-medium ${small ? 'px-1.5 py-0 text-[11px]' : 'px-2 py-0.5 text-[12px]'}`}
      style={{ backgroundColor: `#${color}`, color: readableText(color) }}
    >
      {name}
    </span>
  )
}
