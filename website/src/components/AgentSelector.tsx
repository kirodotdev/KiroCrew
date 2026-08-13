import { useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { Bot, Check } from 'lucide-react'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useProvider } from '../providers'
import { Input, Btn } from './ui'
import { SourceBadge } from './SourceBadge'

import { i18nT } from '../i18n/t'
export interface KiroCrewAgent {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  /** This agent's own default model. '' means inherit (kiro template pin, then
   *  the global fallback). Optional: older payloads predate the field. */
  model?: string
  description: string
  /** Free-text routing intent read by the orchestrator's select_crew. Optional:
   *  older payloads predate the field, and it falls back to `description`. */
  triggers?: string
  source: string
}

interface Props {
  agents: KiroCrewAgent[]
  defaultAgent: string
  value: string
  onChange: (name: string) => void
}

/** Reusable agent selector dropdown with portal positioning. */
export default function AgentSelector({ agents, defaultAgent, value, onChange }: Props) {
  const provider = useProvider()
  const btnRef = useRef<HTMLButtonElement>(null)

  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } = useFilteredDropdown(agents)

  const active = value || defaultAgent || (agents[0]?.name ?? 'default')

  const closeToTrigger = useCallback(() => {
    setOpen(false)
    btnRef.current?.focus()
  }, [setOpen])

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput: true,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => {
      const a = filtered[0]
      if (!a) return
      onChange(a.name)
      closeToTrigger()
    },
    closeToTrigger,
  })

  const handleSelect = (a: KiroCrewAgent) => {
    onChange(a.name)
    closeToTrigger()
  }

  return (
    <div className="relative">
      <button
        ref={btnRef}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-mono font-medium border border-border bg-bg-elevated text-text hover:border-border-strong transition-all cursor-pointer"
        onClick={() => setOpen(!open)}
        aria-label={i18nT('components.agentSelector.switch_agent')}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="text-accent"><Bot size={14} /></span> {active}
        <span className="text-muted text-[11px] ml-1">▾</span>
      </button>
      {open && btnRef.current && createPortal(
        // Presentational positioning wrapper: the interactive semantics live on
        // the inner role="listbox" and its option buttons. This element only
        // hosts the roving-focus keydown handler for the composite widget, so it
        // has no ARIA role of its own.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div
          ref={dropdownRef}
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[240px] max-w-[340px] max-h-[280px] flex flex-col overflow-hidden animate-slide-up"
          style={(() => {
            const r = btnRef.current!.getBoundingClientRect()
            const dropH = 280
            const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4
            const left = Math.max(8, Math.min(r.left, window.innerWidth - 348))
            return { top, left }
          })()}
        >
          <div className="p-2 border-b border-border">
            <Input
              ref={inputRef}
              type="text"
              aria-label={i18nT('components.agentSelector.filter_agents')}
              placeholder={i18nT('components.agentSelector.type_to_filter')}
              value={filter}
              onChange={e => setFilter(e.target.value)}
              className="w-full px-2 py-1 text-[13px]"
            />
          </div>
          <div role="listbox" aria-label={i18nT('components.agentSelector.agent_list')} className="flex-1 min-h-0 overflow-y-auto divide-y divide-border">
            {filtered.map(a => {
              const isCurrent = active === a.name
              const isDefault = a.name === defaultAgent
              return (
                <Btn
                  key={a.name}
                  role="option"
                  aria-selected={isCurrent}
                  tabIndex={-1}
                  className={`w-full text-left px-3 py-2 flex items-center gap-2 min-w-0 border-0 rounded-none cursor-pointer
                    ${isCurrent ? 'bg-accent-subtle hover:bg-accent-subtle' : 'hover:bg-bg-hover'}
                  `}
                  onClick={() => handleSelect(a)}
                >
                  <div className="flex flex-col min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className={`text-[13px] font-mono font-semibold truncate ${isCurrent ? 'text-accent' : 'text-text'}`}>{a.name}</span>
                      {isDefault && <span className="px-1.5 py-[1px] rounded-full text-[10px] font-bold bg-accent-subtle text-accent border border-accent/30 shrink-0">{i18nT('components.agentSelector.default')}</span>}
                      {a.source && (
                        <SourceBadge source={a.source} className="shrink-0">
                          {a.source}
                        </SourceBadge>
                      )}
                    </div>
                    <span className="text-[11px] text-muted truncate">{a.description || provider.resolveAgentTemplate(a)}</span>
                  </div>
                  {isCurrent && <span className="text-accent text-[11px] ml-auto shrink-0"><Check className="lucide-inline" /></span>}
                </Btn>
              )
            })}
            {filtered.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.agentSelector.no_matches')}</div>}
          </div>
        </div>,
        document.body
      )}
    </div>
  )
}
