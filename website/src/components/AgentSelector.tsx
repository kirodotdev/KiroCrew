import { useRef, useCallback, useState } from 'react'
import { createPortal } from 'react-dom'
import { Bot, Check, AlertTriangle } from 'lucide-react'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useProvider } from '../providers'
import { Input, Btn } from './ui'
import { SourceBadge } from './SourceBadge'
import Clickable from './Clickable'

export interface KiroCrewAgent {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  description: string
  source: string
  project_path?: string
  project_name?: string
  project_state?: string // "ok" | "not_found"
}

interface Props {
  agents: KiroCrewAgent[]
  defaultAgent: string
  value: string
  onChange: (name: string, projectPath?: string) => void
  /** If provided, agents from other projects are grayed and show modal */
  activeProjectPath?: string
}

// ── Modal for switching to a different project ──
function SwitchProjectModal({ agent, onSwitch, onCancel }: {
  agent: KiroCrewAgent
  onSwitch: () => void
  onCancel: () => void
}) {
  const folderName = agent.project_name || agent.project_path?.split('/').pop() || ''
  return (
    <Clickable className="fixed inset-0 z-[99999] flex items-center justify-center bg-black/40" onClick={e => { if (!e || e.target === e.currentTarget) onCancel() }}>
      <div role="dialog" aria-modal="true" aria-label="Switch project"
        className="bg-bg-elevated border border-border rounded-xl shadow-xl max-w-[360px] w-full mx-4 p-5">
        <h3 className="text-[14px] font-semibold text-text mb-2">Switch project?</h3>
        <p className="text-[13px] text-muted mb-4">
          <span className="font-mono font-semibold text-text">{agent.name}</span> belongs to{' '}
          <span className="font-mono text-ok">{folderName}</span>.
          Selecting it will switch to that project.
        </p>
        <div className="flex gap-2 justify-end">
          <Btn onClick={onCancel}>Cancel</Btn>
          <Btn primary onClick={onSwitch}>Switch to {folderName} &amp; use</Btn>
        </div>
      </div>
    </Clickable>
  )
}

/** Reusable agent selector dropdown with portal positioning. */
export default function AgentSelector({ agents, defaultAgent, value, onChange, activeProjectPath }: Props) {
  const provider = useProvider()
  const btnRef = useRef<HTMLButtonElement>(null)
  const [pendingAgent, setPendingAgent] = useState<KiroCrewAgent | null>(null)

  // Filter agents: match name OR project folder name
  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered: rawFiltered } = useFilteredDropdown(agents)

  // Custom filter: match name or project folder name
  const filtered = filter
    ? agents.filter(a => {
        const lf = filter.toLowerCase()
        const folderName = (a.project_name || a.project_path?.split('/').pop() || '').toLowerCase()
        return a.name.toLowerCase().includes(lf) || folderName.includes(lf)
      })
    : rawFiltered

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
      if (a.project_path && a.project_path !== activeProjectPath) {
        setPendingAgent(a)
      } else {
        onChange(a.name, a.project_path)
        closeToTrigger()
      }
    },
    closeToTrigger,
  })

  const handleSelect = (a: KiroCrewAgent) => {
    const isOtherProject = a.source === 'project' && a.project_path && a.project_path !== activeProjectPath
    if (isOtherProject) {
      setPendingAgent(a)
      return
    }
    onChange(a.name, a.project_path)
    closeToTrigger()
  }

  // Sort: current-project agents first, then globals, then other-project (grayed)
  const sortedFiltered = [...filtered].sort((a, b) => {
    const aIsCurrentProject = a.source === 'project' && a.project_path === activeProjectPath
    const bIsCurrentProject = b.source === 'project' && b.project_path === activeProjectPath
    const aIsGlobal = a.source !== 'project'
    const bIsGlobal = b.source !== 'project'
    const aIsOther = a.source === 'project' && a.project_path !== activeProjectPath
    const bIsOther = b.source === 'project' && b.project_path !== activeProjectPath
    if (aIsCurrentProject && !bIsCurrentProject) return -1
    if (bIsCurrentProject && !aIsCurrentProject) return 1
    if (aIsGlobal && bIsOther) return -1
    if (bIsGlobal && aIsOther) return 1
    return a.name.localeCompare(b.name)
  })

  return (
    <>
      <div className="relative">
        <button
          ref={btnRef}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-mono font-medium border border-border bg-bg-elevated text-text hover:border-border-strong transition-all cursor-pointer"
          onClick={() => setOpen(!open)}
          aria-label="Switch agent"
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
                aria-label="Filter agents"
                placeholder="Type to filter…"
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="w-full px-2 py-1 text-[13px] font-mono"
              />
            </div>
            <div role="listbox" aria-label="Agent list" className="flex-1 min-h-0 overflow-y-auto divide-y divide-border">
              {sortedFiltered.map(a => {
                const isCurrent = active === a.name && (a.project_path || '') === (activeProjectPath || '')
                const isDefault = a.name === defaultAgent
                const isOtherProject = a.source === 'project' && a.project_path && a.project_path !== activeProjectPath
                const isNotFound = a.project_state === 'not_found'
                const folderName = a.project_name || a.project_path?.split('/').pop() || ''
                return (
                  <Btn
                    key={a.project_path ? `${a.name}:${a.project_path}` : a.name}
                    role="option"
                    aria-selected={isCurrent}
                    tabIndex={-1}
                    disabled={isNotFound}
                    title={isNotFound ? 'Project path not found. Rescan to restore.' : isOtherProject ? `Switch to ${folderName} to use` : undefined}
                    className={`w-full text-left px-3 py-2 flex items-center gap-2 min-w-0 border-0 rounded-none
                      ${isCurrent ? 'bg-accent-subtle hover:bg-accent-subtle' : (!isOtherProject && !isNotFound) ? 'hover:bg-bg-hover' : ''}
                      ${(isOtherProject || isNotFound) ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                    `}
                    onClick={() => handleSelect(a)}
                  >
                    <div className="flex flex-col min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className={`text-[13px] font-mono font-semibold truncate ${isCurrent ? 'text-accent' : 'text-text'}`}>{a.name}</span>
                        {isDefault && <span className="px-1.5 py-[1px] rounded-full text-[10px] font-bold bg-accent-subtle text-accent border border-accent/30 shrink-0">default</span>}
                        {a.source && (
                          <SourceBadge source={a.source} className="shrink-0">
                            {a.source === 'project' && folderName ? `project (${folderName})` : a.source}
                            {isNotFound && <> <AlertTriangle className="lucide-inline text-warn" /></>}
                          </SourceBadge>
                        )}
                      </div>
                      <span className="text-[11px] text-muted truncate">{a.description || provider.resolveAgentTemplate(a)}</span>
                    </div>
                    {isCurrent && <span className="text-accent text-[11px] ml-auto shrink-0"><Check className="lucide-inline" /></span>}
                  </Btn>
                )
              })}
              {sortedFiltered.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">No matches</div>}
            </div>
          </div>,
          document.body
        )}
      </div>
      {pendingAgent && (
        <SwitchProjectModal
          agent={pendingAgent}
          onSwitch={() => { onChange(pendingAgent.name, pendingAgent.project_path); setPendingAgent(null); closeToTrigger() }}
          onCancel={() => setPendingAgent(null)}
        />
      )}
    </>
  )
}
