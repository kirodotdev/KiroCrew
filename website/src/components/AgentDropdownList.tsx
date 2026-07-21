import { useRef, useEffect } from 'react'
import { SourceBadge } from './SourceBadge'
import { Star, Check } from 'lucide-react'

export interface AgentItem {
  name: string
  source: string
  description?: string
}

// ── Single agent row ──
function AgentButton({ a, active, isDefault, activeRef, onSelect, filter }: {
  a: AgentItem
  active: boolean
  isDefault: boolean
  activeRef: React.RefObject<HTMLButtonElement>
  onSelect: (name: string) => void
  filter?: string
}) {
  // Highlight filter matches in text
  const highlight = (text: string) => {
    if (!filter || !text) return text
    const idx = text.toLowerCase().indexOf(filter.toLowerCase())
    if (idx === -1) return text
    return <>{text.slice(0, idx)}<mark className="bg-warn/30 text-text rounded-sm">{text.slice(idx, idx + filter.length)}</mark>{text.slice(idx + filter.length)}</>
  }

  return (
    <button
      ref={active ? activeRef : undefined}
      role="option"
      aria-selected={active}
      tabIndex={-1}
      className={`w-full text-left px-2.5 py-2 flex flex-col gap-0.5 rounded-md transition-all cursor-pointer
        ${active ? 'list-selected bg-accent-subtle' : 'hover:bg-bg-hover'}
      `}
      onClick={() => onSelect(a.name)}
    >
      <div className="flex items-center gap-2">
        <span className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>
          {highlight(a.name)}
        </span>
        <SourceBadge source={a.source}>
          {highlight(a.source)}
          {isDefault ? <> <Star className="lucide-inline" /></> : ''}
        </SourceBadge>
        {active && <span className="text-accent text-[12px]"><Check className="lucide-inline" /></span>}
      </div>
      {a.description && (
        <span className="text-[12px] text-muted leading-tight line-clamp-2" title={a.description}>
          {a.description}
        </span>
      )}
    </button>
  )
}

/** Shared agent list used in dropdown portals across ChatPage and AgentsPage */
export default function AgentDropdownList({ agents, activeAgent, defaultAgent, onSelect, filter }: {
  agents: AgentItem[]
  activeAgent: string
  defaultAgent: string
  onSelect: (name: string) => void
  filter?: string
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])

  if (agents.length === 0) {
    return <div className="px-3 py-2 text-[13px] text-muted italic">No matches</div>
  }

  return (
    <div className="overflow-y-auto flex flex-col max-h-[300px]">
      {agents.map(a => {
        const active = activeAgent === a.name
        return <AgentButton key={a.name} a={a} active={active} isDefault={a.name === defaultAgent} activeRef={activeRef} onSelect={onSelect} filter={filter} />
      })}
    </div>
  )
}
