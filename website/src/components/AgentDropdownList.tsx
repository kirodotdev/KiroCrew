import { useRef, useEffect } from 'react'
import { Trans } from 'react-i18next'
import { SourceBadge } from './SourceBadge'
import ErrorNotice from './ErrorNotice'
import { PanelSectionHeader } from './ui'
import CrewAvatar from './CrewAvatar'
import { Star, Check, Users } from 'lucide-react'

import { i18nT } from '../i18n/t'

export type AgentItemKind = 'member' | 'template'

export interface AgentItem {
  name: string
  source: string
  description?: string
  /** The namespace this row came from. A member and a template may share a
   *  `name`; when any row carries a kind the list groups by it and keys rows
   *  on (kind, name). Absent on a name-only roster, which renders flat. */
  selection_kind?: AgentItemKind
  /** The crewmate's `avatar` record, verbatim from the roster row. Only a
   *  member row has one; a template is a definition, not a crewmate, and
   *  wears no face. */
  avatar?: unknown
}

/** Row identity for React keys and the active match: JSON so no separator a
 *  name could contain can collide two rows. */
const itemKey = (a: AgentItem) => JSON.stringify([a.selection_kind ?? '', a.name])

// ── Single agent row ──
function AgentButton({ a, active, isDefault, showSource, activeRef, onSelect, filter }: {
  a: AgentItem
  active: boolean
  isDefault: boolean
  /** The flat (name-only) list tells rows apart by origin; the grouped list
   *  already says what a row IS in its header, so the badge is noise there. */
  showSource: boolean
  activeRef: React.RefObject<HTMLButtonElement>
  onSelect: (name: string, kind?: AgentItemKind) => void
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
      // A name-only row keeps the one-argument call its callers were written for.
      onClick={() => (a.selection_kind ? onSelect(a.name, a.selection_kind) : onSelect(a.name))}
    >
      <div className="flex items-center gap-2">
        {a.selection_kind === 'member' && (
          // The same face the Crewmates roster draws for this crewmate, so the
          // picker row and the roster row read as one identity.
          <CrewAvatar seed={a.name} avatar={a.avatar} size={20} className="shrink-0" />
        )}
        <span className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>
          {highlight(a.name)}
        </span>
        {isDefault ? (
          <span className="shrink-0 inline-flex items-center gap-1 px-1.5 py-[1px] rounded-full text-[11px] font-semibold bg-warn-subtle text-warn" title={i18nT('components.agentDropdownList.new_sessions_start_with_this_agent')}>
            <Star className="lucide-inline" />{i18nT('components.agentDropdownList.default')}
          </span>
        ) : showSource ? (
          <SourceBadge source={a.source}>{highlight(a.source)}</SourceBadge>
        ) : null}
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
      className="shrink-0 border-t border-border flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover focus:text-text focus:bg-bg-hover focus:outline-hidden focus-ring transition-colors disabled:cursor-default disabled:hover:bg-transparent"
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
        <div className="shrink-0 border-t border-border px-2 py-2">
          {/* Pop-up holds no draft (the default-agent write already fired), so the hand-off loses nothing.
              Block variant: the pop-up is narrow, so the wrapped message and the link stack instead of
              fighting for one line. */}
          <ErrorNotice
            askAgent
            testId="agent-dropdown-default-error"
            message={i18nT('components.agentDropdownList.could_not_change_the_default_agent')}
          />
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

/** Shared agent list used in dropdown portals across ChatPage and ChatPane.
 *
 *  `activeKind` is the namespace the slot's current agent was chosen in. With
 *  it, a same-name member and template light up separately; without it (a
 *  slot restored from history, an older gateway) the match falls back to the
 *  name alone rather than guessing a namespace the backend never recorded. */
export default function AgentDropdownList({ agents, activeAgent, activeKind, defaultAgent, onSelect, filter }: {
  agents: AgentItem[]
  activeAgent: string
  activeKind?: AgentItemKind | ''
  defaultAgent: string
  onSelect: (name: string, kind?: AgentItemKind) => void
  filter?: string
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])

  if (agents.length === 0) {
    return <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.agentDropdownList.no_matches')}</div>
  }

  // A slot that recorded no namespace runs whatever the backend's name-first
  // resolution answers for the bare name: the member when one exists, else the
  // template. Lighting that row is reading the contract, not guessing.
  const hasMember = (name: string) => agents.some(m => m.name === name && m.selection_kind === 'member')
  const isActive = (a: AgentItem) => {
    if (activeAgent !== a.name) return false
    if (!a.selection_kind) return true
    if (activeKind) return activeKind === a.selection_kind
    return a.selection_kind === 'member' || !hasMember(a.name)
  }
  const grouped = agents.some(a => a.selection_kind)
  // A header earns its place only when it separates something. With one kind
  // in the list (crewmates withheld by `HIDE_CREWMATE_CHOICES`, or an install
  // with no templates) the header and the templates hint would name a
  // distinction the list does not draw, so both are dropped; the `role="group"`
  // label stays for assistive technology, which does not read the chrome.
  const kinds = new Set(agents.map(a => a.selection_kind ?? 'member'))
  const showGroupChrome = kinds.size > 1
  const row = (a: AgentItem) => (
    <AgentButton key={itemKey(a)} a={a} active={isActive(a)} isDefault={a.name === defaultAgent && a.selection_kind !== 'template'} showSource={!grouped} activeRef={activeRef} onSelect={onSelect} filter={filter} />
  )

  return (
    // Plain flex column: scrolling is owned by the host's listbox wrapper
    // (ChatPage / ChatPane render this inside `overflow-y-auto max-h-[280px]`).
    // A second overflow container here nests two scrollbars (#6375), and
    // `scrollIntoView` on the active row scrolls the nearest scrollable
    // ancestor, so the host-owned scroller keeps that behavior intact.
    // role="presentation" keeps this layout div out of the listbox's
    // owned-children chain (hosts put role="listbox" on their wrapper).
    <div role="presentation" className="flex flex-col">
      {!grouped
        ? agents.map(row)
        : (['member', 'template'] as const).map(kind => {
          const rows = agents.filter(a => (a.selection_kind ?? 'member') === kind)
          if (!rows.length) return null
          const label = kind === 'member'
            ? i18nT('components.agentDropdownList.group_members')
            : i18nT('components.agentDropdownList.group_templates')
          return (
            <div key={kind} role="group" aria-label={label} className="flex flex-col">
              {showGroupChrome && (
                <PanelSectionHeader label={label} count={rows.length} className="px-2.5 pt-2 pb-1" />
              )}
              {showGroupChrome && kind === 'template' && (
                // What a template pick IS, said where the pick happens: it runs the
                // shared template on the shared default memory and enrols nothing.
                <p className="px-2.5 pb-1 text-[11px] leading-snug text-muted">
                  {i18nT('components.agentDropdownList.group_templates_hint')}
                </p>
              )}
              {rows.map(row)}
            </div>
          )
        })}
    </div>
  )
}
