/**
 * The offering agent's own words inside a guide: the guide's `intro` (offer
 * card, first step) or one action's `note` (under its final step).
 *
 * Always drawn BELOW the dashboard's own template line, so the person can tell
 * what the dashboard says (where, which control) from what the agent adds (why
 * it matters). Attributed to the crewmate whose thread offered the guide
 * (`slotKey`, matched against the roster's pinned threads); a guide offered from
 * an ordinary chat carries no "From" line, since that chat has no crewmate name
 * to show. Rendered as React text: the gateway already refused links and
 * markup, and nothing here interprets the string. Used only by the guide offer
 * card and the guide panel.
 */
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { membersRosterQuery } from '../api/membersQuery'
import { isAssistantMember } from '../lib/assistantMember'

export default function GuideMateNote({ text, testId, slotKey, className = '' }: {
  text: string | undefined | null
  testId: string
  slotKey: string | null | undefined
  className?: string
}) {
  const { t } = useTranslation()
  const roster = useQuery({ ...membersRosterQuery, enabled: !!slotKey })
  const body = typeof text === 'string' ? text.trim() : ''
  if (!body) return null
  const row = slotKey ? roster.data?.find(r => !!r.slot_key && r.slot_key === slotKey) : undefined
  const label = row?.display_name?.trim()
  const name = row
    ? label || (isAssistantMember(row) ? t('components.assistantWelcome.default_name') : row.name)
    : ''
  return (
    <p className={`m-0 border-s-2 border-border ps-2 text-[12px] leading-4 text-muted break-words ${className}`} data-testid={testId}>
      {name && <span className="block font-medium text-text" data-testid={`${testId}-from`}>{t('components.guideLayer.note_from', { name })}</span>}
      <span className="block">{body}</span>
    </p>
  )
}
