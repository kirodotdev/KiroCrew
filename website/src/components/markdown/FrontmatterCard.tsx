// Render a markdown document's YAML frontmatter as a collapsible key/value
// metadata strip in the file preview. Every surface sets a background WITH its
// text color from theme tokens so it reads in both light and dark; no fixed
// palette.
import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import type { FrontmatterEntry } from './frontmatter'

export function FrontmatterCard({ entries }: { entries: FrontmatterEntry[] }) {
  const [open, setOpen] = useState(true)
  if (entries.length === 0) return null
  return (
    <div className="mb-4 rounded-md border border-border bg-bg-elevated text-text overflow-hidden" data-testid="md-frontmatter">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 w-full text-left px-2.5 py-1.5 text-[12px] font-medium text-muted hover:text-text bg-transparent border-none cursor-pointer"
        aria-expanded={open}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span>{i18nT('components.frontmatterCard.heading', { count: entries.length })}</span>
      </button>
      {open && (
        <dl className="px-2.5 pb-2 pt-0.5 grid grid-cols-[minmax(0,max-content)_1fr] gap-x-3 gap-y-1 text-[12.5px]">
          {entries.map((e) => (
            <div key={e.key} className="contents">
              <dt className="font-mono text-accent whitespace-nowrap">{e.key}</dt>
              <dd className="font-mono text-text break-words whitespace-pre-wrap min-w-0">{e.value || <span className="text-muted">∅</span>}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
