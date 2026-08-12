import { useState } from 'react'
import { Lock, ShieldAlert } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { slotChannelNamespace } from '../utils/channelOrigin'
import { connectionForTransport, isRestricted, useConnections } from '../hooks/useConnections'
import { ChannelBrandIcon, hasChannelBrandIcon } from './ChannelBrandIcon'

/**
 * Header chip for a session driven by a chat CONNECTION whose reach is bounded.
 *
 * Sits beside `InboundLinkChip` and follows the same reasoning that component
 * documents for itself: a property of the session that is otherwise invisible
 * gets a persistent chip rather than living in a menu the user has to open. Here
 * the invisible property is that the conversation arrives through a credentialed
 * principal that governance constrains — the transcript looks like any other tab.
 *
 * Renders NOTHING for a dashboard session and nothing for an unconstrained
 * connection: one that is enrolled, permitted, and has no pinned sender list
 * carries no surprise, so announcing it would be noise on every channel session.
 */
export default function ConnectionChip({ slotKey }: { slotKey?: string }) {
  const [open, setOpen] = useState(false)
  const { data } = useConnections()
  const transport = slotChannelNamespace(slotKey)
  const connection = connectionForTransport(data, transport)

  if (!slotKey || !transport || !connection) return null
  if (!isRestricted(connection)) return null

  // A roster that could not be READ is not an operator decision, so it gets its
  // own wording: saying "not enrolled" would blame the operator for a file the
  // gateway could not open. Order matters — an unreadable roster makes every
  // connection read as unenrolled, so it must be checked first.
  const headline = data?.roster.loaded === false
    ? i18nT('components.connectionChip.roster_unreadable')
    : !connection.enrolled
      ? i18nT('components.connectionChip.not_enrolled')
      : connection.permitted === false
        ? i18nT('components.connectionChip.denied_by_policy')
        : i18nT('components.connectionChip.senders_pinned')

  return (
    <span className="pointer-events-auto relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-2 py-0.5 text-[11px] text-muted cursor-pointer hover:bg-bg-hover transition-colors"
        title={i18nT('components.connectionChip.tooltip', { id: connection.id })}
      >
        <Lock size={11} className="shrink-0" aria-hidden />
        {hasChannelBrandIcon(transport) && <ChannelBrandIcon channel={transport} size={11} />}
        <span className="font-mono max-w-[20ch] truncate">{connection.id}</span>
        <span className="text-text">{i18nT('components.connectionChip.restricted')}</span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label={i18nT('components.connectionChip.aria_detail', { id: connection.id })}
          className="absolute top-full left-0 z-50 mt-1.5 w-[310px] rounded-lg border border-border-strong bg-bg-elevated p-3 shadow-lg"
        >
          <div className="flex items-start gap-2">
            <ShieldAlert size={14} className="mt-0.5 shrink-0 text-muted" aria-hidden />
            <div className="min-w-0">
              <div className="font-mono text-[12.5px] font-semibold text-text-strong">
                {connection.id}
              </div>
              <div className="mt-0.5 text-[11.5px] text-muted leading-relaxed">{headline}</div>
            </div>
          </div>
          <p className="mt-2.5 border-t border-border pt-2 text-[11.5px] text-muted leading-relaxed">
            {connection.senders_pinned
              ? i18nT('components.connectionChip.senders_from_policy')
              : i18nT('components.connectionChip.senders_from_config')}
          </p>
          <p className="mt-1.5 text-[11px] text-muted leading-relaxed">
            {i18nT('components.connectionChip.read_only_note')}
          </p>
        </div>
      )}
    </span>
  )
}
