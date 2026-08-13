import { useRef, useEffect } from 'react'
import { Trans } from 'react-i18next'
import { SourceBadge } from './SourceBadge'
import { Star, Check, Users } from 'lucide-react'

import { i18nT } from '../i18n/t'
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
 * Footer row that promotes an agent to the global default, mirroring the model pop-up's
 * own pin row. It acts on the agent the row selection has already made active, which is
 * what lets the label name the exact agent it writes — a bare icon can only put that in
 * a tooltip, and this pop-up's other job is switching the agent for THIS session, so an
 * unqualified "default" reads as session-scoped.
 *
 * Set-only: once an agent holds the default the row reports that state instead of
 * offering a no-op write, and clearing lives on the Agent Templates page, where the
 * control is labelled and the outcome is visible in a summary card.
 */
export function DefaultAgentRow({ agentName, isDefault, onSetDefault }: {
  agentName: string
  isDefault: boolean
  onSetDefault: () => void
}) {
  return (
    <button
      type="button"
      onClick={isDefault ? undefined : onSetDefault}
      disabled={isDefault}
      aria-pressed={isDefault}
      // `data-option` + tabIndex enrol the actionable row in the listbox's
      // roving-focus ring (`useListboxKeyboard` moves real focus across
      // `[data-option],[role="option"]`). Without it the row is pointer-only: the
      // hook consumes Tab to close the pop-up, so a plain button in the footer can
      // never receive focus. Enter/Space then work natively — the hook leaves a
      // focused option's activation to the button itself. Omitted while disabled,
      // so the ring never stops on a row that cannot be actuated.
      {...(isDefault ? {} : { 'data-option': true, tabIndex: -1 })}
      className="shrink-0 border-t border-border flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover focus:text-text focus:bg-bg-hover focus:outline-none focus-ring transition-colors disabled:cursor-default disabled:hover:bg-transparent"
    >
      {/* Wraps rather than truncates. The label's whole job is to name WHICH agent the
          write targets, and that identifier sits mid-string — an ellipsis eats exactly
          the part that carries the meaning. The pop-up caps at 340px and agent names
          are unbounded, so a second line has to be free rather than clipped. */}
      <span className="min-w-0 text-left break-words">
        {isDefault
          ? i18nT('components.agentDropdownList.default_agent_for_new_sessions')
          : <Trans
              i18nKey="components.agentDropdownList.set_default_agent"
              components={{ agent: <span className="font-mono">{agentName}</span> }}
            />}
      </span>
      {isDefault ? <Check size={13} className="text-accent" /> : <Star size={13} />}
    </button>
  )
}

/**
 * Footer link out of an agent pop-up to the Agent Templates page. Mirrors the model
 * pop-up's own "Set default for new sessions…" footer so the two pickers agree on where
 * a picker sends you to change what it is picking from.
 *
 * `error` renders the failed-write line: the default-agent write is fire-and-forget, so
 * without it a rejected request is indistinguishable from a successful one. It sits here
 * rather than on `DefaultAgentRow` so the alert lands directly beneath the control that
 * failed.
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
    return <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.agentDropdownList.no_matches')}</div>
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
