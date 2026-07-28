import { useRef, useEffect } from 'react'
import { Check } from 'lucide-react'

import { i18nT } from '../i18n/t'
interface ModelItem { name: string; description?: string }

/** Shared model list used in dropdown portals across AgentsPage and ChatPage */
export default function ModelDropdownList({ models, activeModel, onSelect }: {
  models: ModelItem[]; activeModel: string; onSelect: (name: string) => void
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])
  return (
    <div className="overflow-y-auto flex flex-col gap-0.5">
      {models.map(m => {
        const active = activeModel === m.name
        return (
          <button key={m.name} ref={active ? activeRef : undefined} role="option" aria-selected={active} tabIndex={-1} className={`w-full text-left px-2.5 py-2 flex flex-col gap-0.5 rounded-md cursor-pointer transition-all border-none bg-transparent ${active ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`} onClick={() => onSelect(m.name)}>
            <div className="flex items-center gap-2">
              <span className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>{m.name}</span>
              {active && <span className="text-accent text-[12px]"><Check className="lucide-inline" /></span>}
            </div>
            {m.description && <span className="text-[12px] text-muted leading-tight">{m.description}</span>}
          </button>
        )
      })}
      {models.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.modelDropdownList.no_matches')}</div>}
    </div>
  )
}
