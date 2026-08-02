import { useRef, useEffect } from 'react'
import { SourceBadge } from './SourceBadge'
import { Star, Check, Users } from 'lucide-react'

import { i18nT } from '../i18n/t'
export interface AgentItem {
  name: string
  source: string
  description?: string
}

// ── Single agent row ──
function AgentButton({ a, active, isDefault, activeRef, onSelect, onSetDefault, filter }: {
  a: AgentItem
  active: boolean
  isDefault: boolean
  activeRef: React.RefObject<HTMLButtonElement>
  onSelect: (name: string) => void
  onSetDefault?: (name: string) => void
  filter?: string
}) {
  const highlight = (text: string) => {
    if (!filter || !text) return text
    const idx = text.toLowerCase().indexOf(filter.toLowerCase())
    if (idx === -1) return text
    return <>{text.slice(0, idx)}<mark className="bg-warn/30 text-text rounded-sm">{text.slice(idx, idx + filter.length)}</mark>{text.slice(idx + filter.length)}</>
  }

  // Scope is named in full here rather than as a bare "Set as default": this pop-up's
  // other job is switching the agent for THIS session, so an unqualified "default"
  // reads as session-scoped when the setting is global.
  const setDefaultLabel = i18nT('components.agentDropdownList.start_new_sessions_with_this_agent')

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
        {isDefault ? (
          <span className="shrink-0 inline-flex items-center gap-1 px-1.5 py-[1px] rounded-full text-[11px] font-semibold bg-warn-subtle text-warn" title={i18nT('components.agentDropdownList.new_sessions_start_with_this_agent')}>
            <Star className="lucide-inline" />{i18nT('components.agentDropdownList.default')}
          </span>
        ) : (
          <SourceBadge source={a.source}>{highlight(a.source)}</SourceBadge>
        )}
        {active && (
          <span className="text-accent text-[12px]" title={i18nT('components.agentDropdownList.active_in_this_session')}>
            <Check className="lucide-inline" />
          </span>
        )}
        {onSetDefault && !isDefault && (
          // Nested interactive control: the row selects the agent for this session,
          // while this promotes it to the global default. stopPropagation keeps the two
          // apart. Rendered as a span because a <button> cannot nest in a <button>.
          //
          // `data-option` enrols it in the listbox's roving-focus ring
          // (useListboxKeyboard matches `[data-option],[role="option"]`), so Arrow
          // keys reach it and its own Enter/Space handler invokes it. Without that it
          // was pointer-only, which made the setting unreachable for a keyboard user.
          //
          // Only offered on rows that are NOT already the default: making the lit star
          // clear the default put "leave the product with no default agent at all"
          // behind the same gesture that sets one, with no confirmation. Clearing stays
          // on the Templates page, where the control is labelled and the result is
          // visible in a summary card.
          <span
            role="button"
            data-option
            tabIndex={-1}
            aria-label={setDefaultLabel}
            title={setDefaultLabel}
            className="ml-auto shrink-0 flex items-center justify-center w-[22px] h-[22px] rounded-[5px] text-muted-strong hover:bg-warn-subtle hover:text-warn focus:bg-warn-subtle focus:text-warn focus:outline-none focus-ring transition-colors"
            onClick={e => { e.stopPropagation(); onSetDefault(a.name) }}
            onKeyDown={e => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onSetDefault(a.name) }
            }}
          >
            <Star className="lucide-inline" />
          </span>
        )}
      </div>
      {a.description && (
        <span className="text-[12px] text-muted leading-tight line-clamp-2" title={a.description}>
          {a.description}
        </span>
      )}
    </button>
  )
}

/**
 * Footer link out of an agent pop-up to the Agent Templates page. Mirrors the model
 * pop-up's own "Set default for new sessions…" footer so the two pickers agree on where
 * a picker sends you to change what it is picking from.
 *
 * `error` renders the failed-write line: the default-agent write is fire-and-forget, so
 * without it a rejected request is indistinguishable from a successful one.
 */
export function ManageAgentsFooter({ onManage, error }: { onManage: () => void; error?: boolean }) {
  return (
    <>
      {error && (
        <div role="alert" className="shrink-0 border-t border-border px-3 py-2 text-[12px] text-danger">
          {i18nT('components.agentDropdownList.could_not_change_the_default_agent')}
        </div>
      )}
      <button
        type="button"
        onClick={onManage}
        className="shrink-0 border-t border-border rounded-b-lg flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover transition-colors"
      >
        <span>{i18nT('components.agentDropdownList.manage_agents')}</span>
        <Users className="lucide-inline" />
      </button>
    </>
  )
}

/** Shared agent list used in dropdown portals across ChatPage and AgentsPage */
export default function AgentDropdownList({ agents, activeAgent, defaultAgent, onSelect, onSetDefault, filter }: {
  agents: AgentItem[]
  activeAgent: string
  defaultAgent: string
  onSelect: (name: string) => void
  /** Omit to render the list read-only (no per-row default toggle). */
  onSetDefault?: (name: string) => void
  filter?: string
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])

  if (agents.length === 0) {
    return <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.agentDropdownList.no_matches')}</div>
  }

  return (
    <div className="overflow-y-auto flex flex-col max-h-[300px]">
      {agents.map(a => {
        const active = activeAgent === a.name
        return <AgentButton key={a.name} a={a} active={active} isDefault={a.name === defaultAgent} activeRef={activeRef} onSelect={onSelect} onSetDefault={onSetDefault} filter={filter} />
      })}
    </div>
  )
}
