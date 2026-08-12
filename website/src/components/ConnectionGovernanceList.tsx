import { useState } from 'react'
import { ChevronDown, ChevronRight, ShieldAlert } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { isRestricted, useConnections } from '../hooks/useConnections'
import type { ChannelConnection } from '../types'
import { ChannelBrandIcon, hasChannelBrandIcon } from './ChannelBrandIcon'

/** One connection row: what governs it, expandable to the specifics. */
function ConnectionRow({ connection, rosterLoaded }: {
  connection: ChannelConnection
  rosterLoaded: boolean
}) {
  const [open, setOpen] = useState(false)
  const Chevron = open ? ChevronDown : ChevronRight
  // Same precedence as the session chip: an unreadable roster makes every
  // connection read as unenrolled, so it has to be named first or the operator
  // is told they never enrolled a bot they did.
  const verdict = !rosterLoaded
    ? i18nT('components.connectionGovernanceList.roster_unreadable')
    : !connection.enrolled
      ? i18nT('components.connectionGovernanceList.not_enrolled')
      : connection.permitted === false
        ? i18nT('components.connectionGovernanceList.denied')
        : connection.permitted === null
          ? i18nT('components.connectionGovernanceList.unavailable')
          : i18nT('components.connectionGovernanceList.allowed')

  return (
    <div className="border-t border-border first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 bg-transparent border-none px-0 py-1.5 text-left cursor-pointer"
      >
        <Chevron size={12} className="shrink-0 text-muted" aria-hidden />
        {hasChannelBrandIcon(connection.transport) && (
          <ChannelBrandIcon channel={connection.transport} size={13} />
        )}
        <span className="font-mono text-[11.5px] font-semibold text-text-strong truncate">
          {connection.id}
        </span>
        <span className="ml-auto shrink-0 text-[11px] text-muted">{verdict}</span>
      </button>

      {open && (
        <dl className="mb-1.5 ml-[22px] space-y-0.5 text-[11px]">
          <div className="flex items-center gap-3">
            <dt className="text-muted">
              {i18nT('components.connectionGovernanceList.enrolment')}
            </dt>
            <dd className="ml-auto text-text">
              {connection.enrolled
                ? i18nT('components.connectionGovernanceList.on_the_roster')
                : i18nT('components.connectionGovernanceList.off_the_roster')}
            </dd>
          </div>
          <div className="flex items-center gap-3">
            <dt className="text-muted">
              {i18nT('components.connectionGovernanceList.senders')}
            </dt>
            <dd className="ml-auto text-text">
              {connection.senders_pinned
                ? i18nT('components.connectionGovernanceList.pinned_by_policy')
                : i18nT('components.connectionGovernanceList.from_config')}
            </dd>
          </div>
          {connection.layer && (
            <div className="flex items-center gap-3">
              <dt className="text-muted">
                {i18nT('components.connectionGovernanceList.decided_by')}
              </dt>
              <dd className="ml-auto font-mono text-text">{connection.layer}</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  )
}

/**
 * Every chat connection this instance admits, and what bounds each one.
 *
 * Replaces a single sentence that could only NAME the surfaces carrying their own
 * ceiling. Naming them was the right instinct with nowhere to put the content:
 * an operator who reads "telegram has its own profile" still cannot see whether
 * that bot is enrolled, what the ceiling decided, or who may talk to it — which
 * is the whole question when several bots share one instance.
 *
 * Read-only by construction. Enrolment lives in the trust roster and the ceiling
 * in the security policy, both keystone-fenced, so this surface reports and never
 * edits.
 */
export default function ConnectionGovernanceList() {
  const { data, isError } = useConnections()
  if (isError || !data || data.enabled === false || data.connections.length === 0) return null

  const rosterLoaded = data.roster.loaded
  // An unrestricted, fully-permitted set of connections is the ordinary state and
  // needs no list: the section above already says the ceiling is open. The list
  // earns its space only when something is actually bounded — or when the roster
  // could not be read, which an operator must see even though nothing is "denied".
  const notable = data.connections.filter(c => isRestricted(c))
  if (rosterLoaded && notable.length === 0) return null

  return (
    <div className="border-t border-border pt-2 mt-2">
      <div className="flex items-start gap-2 mb-1">
        {!rosterLoaded && (
          <ShieldAlert size={13} className="mt-0.5 shrink-0 text-danger" aria-hidden />
        )}
        <p className="text-[11px] text-muted leading-relaxed">
          {rosterLoaded
            ? i18nT('components.connectionGovernanceList.intro')
            : i18nT('components.connectionGovernanceList.intro_roster_unreadable', {
              path: data.roster.path,
            })}
        </p>
      </div>
      <div>
        {(rosterLoaded ? notable : data.connections).map(c => (
          <ConnectionRow key={c.id} connection={c} rosterLoaded={rosterLoaded} />
        ))}
      </div>
    </div>
  )
}
