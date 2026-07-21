import { useRef, useEffect, useState } from 'react'
import { SourceBadge } from './SourceBadge'
import { Star, Check, AlertTriangle } from 'lucide-react'
import { Btn } from './ui'
import Clickable from './Clickable'

export interface AgentItem {
  name: string
  source: string
  description?: string
  project_path?: string
  project_name?: string  // display name from registry
  project_state?: string // "ok" | "not_found"
}

// ── Modal for switching to an agent from a different project ──
function SwitchProjectModal({ agent, onSwitch, onCancel }: {
  agent: AgentItem
  onSwitch: () => void
  onCancel: () => void
}) {
  const folderName = agent.project_name || agent.project_path?.split('/').pop() || ''
  return (
    <Clickable className="fixed inset-0 z-[99999] flex items-center justify-center bg-black/40" onClick={onCancel}>
      {/* Modal panel: the click/key handlers only stop propagation so the */}
      {/* backdrop's dismiss-on-click doesn't fire — they are not user controls. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div className="bg-bg-elevated border border-border rounded-xl shadow-xl max-w-[360px] w-full mx-4 p-5"
        role="dialog"
        aria-modal="true"
        aria-label="Switch project"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}>
        <h3 className="text-[14px] font-semibold text-text mb-2">Switch project?</h3>
        <p className="text-[13px] text-muted mb-4">
          <span className="font-mono font-semibold text-text">{agent.name}</span> belongs to{' '}
          <span className="font-mono text-ok">{folderName}</span>.
          Selecting it will switch your session to that project.
        </p>
        <div className="flex gap-2 justify-end">
          <Btn onClick={onCancel}>Cancel</Btn>
          <Btn primary onClick={onSwitch}>Switch to {folderName} &amp; use</Btn>
        </div>
      </div>
    </Clickable>
  )
}

// ── Single agent row ──
function AgentButton({ a, active, isDefault, activeRef, onSelect, activeProjectPath, filter }: {
  a: AgentItem
  active: boolean
  isDefault: boolean
  activeRef: React.RefObject<HTMLButtonElement>
  onSelect: (name: string, projectPath?: string) => void
  activeProjectPath?: string
  filter?: string
}) {
  const folderName = a.project_name || a.project_path?.split('/').pop() || ''
  const isCurrentProject = a.source === 'project' && a.project_path === activeProjectPath
  const isNotFound = a.project_state === 'not_found'
  const isOtherProject = a.source === 'project' && a.project_path !== activeProjectPath

  const [showModal, setShowModal] = useState(false)

  // Highlight filter matches in text
  const highlight = (text: string) => {
    if (!filter || !text) return text
    const idx = text.toLowerCase().indexOf(filter.toLowerCase())
    if (idx === -1) return text
    return <>{text.slice(0, idx)}<mark className="bg-warn/30 text-text rounded-sm">{text.slice(idx, idx + filter.length)}</mark>{text.slice(idx + filter.length)}</>
  }

  const badgeLabel = a.source === 'project' && folderName
    ? `project (${folderName})`
    : a.source

  const handleClick = () => {
    if (isNotFound) return  // not_found agents: click shows tooltip-like info, not modal
    if (isOtherProject) {
      setShowModal(true)
      return
    }
    onSelect(a.name, a.project_path)
  }

  return (
    <>
      <button
        ref={active ? activeRef : undefined}
        role="option"
        aria-selected={active}
        tabIndex={-1}
        disabled={isNotFound}
        title={isNotFound ? 'Project path not found. Rescan to restore.' : isOtherProject ? `Switch to ${folderName} to use this agent` : undefined}
        className={`w-full text-left px-2.5 py-2 flex flex-col gap-0.5 rounded-md transition-all
          ${isNotFound || isOtherProject ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
          ${active ? 'list-selected bg-accent-subtle' : (!isNotFound && !isOtherProject) ? 'hover:bg-bg-hover' : ''}
        `}
        onClick={handleClick}
      >
        <div className="flex items-center gap-2">
          <span className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>
            {highlight(a.name)}
          </span>
          <SourceBadge source={a.source}>
            {highlight(badgeLabel)}
            {isCurrentProject && !isNotFound ? <> <Star className="lucide-inline" /></> : ''}
            {isDefault ? <> <Star className="lucide-inline" /></> : ''}
            {isNotFound ? <> <AlertTriangle className="lucide-inline text-warn" /></> : ''}
          </SourceBadge>
          {active && <span className="text-accent text-[12px]"><Check className="lucide-inline" /></span>}
        </div>
        {a.description && (
          <span className="text-[12px] text-muted leading-tight line-clamp-2" title={a.description}>
            {a.description}
          </span>
        )}
      </button>
      {showModal && (
        <SwitchProjectModal
          agent={a}
          onSwitch={() => { setShowModal(false); onSelect(a.name, a.project_path) }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </>
  )
}

/** Shared agent list used in dropdown portals across ChatPage and AgentsPage */
export default function AgentDropdownList({ agents, activeAgent, activeProjectPath, defaultAgent, onSelect, filter }: {
  agents: AgentItem[]
  activeAgent: string
  activeProjectPath?: string
  defaultAgent: string
  onSelect: (name: string, projectPath?: string) => void
  filter?: string
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])

  const currentProjectAgents = agents.filter(a =>
    a.source === 'project' && a.project_path === activeProjectPath
  )
  const globalAgents = agents.filter(a => a.source !== 'project')
  const otherProjectAgents = agents.filter(a =>
    a.source === 'project' && a.project_path !== activeProjectPath
  ).sort((a, b) => {
    const aFolder = (a.project_name || a.project_path?.split('/').pop() || '').toLowerCase()
    const bFolder = (b.project_name || b.project_path?.split('/').pop() || '').toLowerCase()
    return aFolder.localeCompare(bFolder) || a.name.localeCompare(b.name)
  })

  const hasCurrentProject = currentProjectAgents.length > 0
  const hasGlobals = globalAgents.length > 0
  const hasOtherProjects = otherProjectAgents.length > 0

  if (agents.length === 0) {
    return (
      <div className="flex flex-col">
        <div className="px-3 py-2 text-[13px] text-muted italic">No matches</div>
        <div className="px-3 pb-2 text-[11px] text-muted">
          <a href="/agents" className="text-ok hover:underline">Scan projects to discover more →</a>
        </div>
      </div>
    )
  }

  return (
    <div className="overflow-y-auto flex flex-col max-h-[300px]">
      {/* Current project agents — selectable */}
      {hasCurrentProject && (
        <div>
          <div className="px-2.5 pt-2 pb-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-ok">Project Agents</span>
          </div>
          {currentProjectAgents.map(a => {
            const key = `${a.name}:${a.project_path || ''}`
            const active = activeAgent === a.name && a.project_path === activeProjectPath
            return <AgentButton key={key} a={a} active={active} isDefault={false} activeRef={activeRef} onSelect={onSelect} activeProjectPath={activeProjectPath} filter={filter} />
          })}
        </div>
      )}

      {/* Divider */}
      {hasCurrentProject && hasGlobals && <div className="mx-2.5 my-1 border-t border-border" />}

      {/* Global agents — selectable */}
      {hasGlobals && (
        <div>
          {hasCurrentProject && (
            <div className="px-2.5 pt-1.5 pb-1">
              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">Global Agents</span>
            </div>
          )}
          {globalAgents.map(a => {
            const active = activeAgent === a.name && !currentProjectAgents.some(p => p.name === a.name)
            return <AgentButton key={a.name} a={a} active={active} isDefault={a.name === defaultAgent} activeRef={activeRef} onSelect={onSelect} filter={filter} />
          })}
        </div>
      )}

      {/* Divider */}
      {hasOtherProjects && (hasCurrentProject || hasGlobals) && <div className="mx-2.5 my-1 border-t border-border" />}

      {/* Other project agents — grayed out */}
      {hasOtherProjects && (
        <div>
          <div className="px-2.5 pt-1.5 pb-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted opacity-60">Other Projects</span>
          </div>
          {otherProjectAgents.map(a => {
            const key = `${a.name}:${a.project_path || ''}`
            return <AgentButton key={key} a={a} active={false} isDefault={false} activeRef={activeRef} onSelect={onSelect} activeProjectPath={activeProjectPath} filter={filter} />
          })}
        </div>
      )}
    </div>
  )
}
