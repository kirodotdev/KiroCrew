import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ConfiguredChannelTarget } from '../types'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'

/** The `channel_type` a job's Telegram destination is saved under. */
export const TELEGRAM_CHANNEL = 'telegram'

/** A private chat's target is `user:<user id>`. */
const USER_PREFIX = 'user:'

const targetList = (result: unknown) => (Array.isArray(result) ? result as ConfiguredChannelTarget[] : [])

/** Where on Telegram a scheduled job reports: the private chat, or one of its
 *  topics the bot already has a conversation in. Renders nothing while Telegram
 *  is not connected, unless the job is already pinned, so the pin can still be
 *  cleared. `value` is the job's `target_id`, `""` for none. */
export default function TelegramDestinationSelect({ value, onChange, labelled = false }: {
  value: string
  onChange: (targetId: string) => void
  /** Show the field's label and hint above it (the form's vertical layout). */
  labelled?: boolean
}) {
  // The key and shape the session menu caches this list under, so both share it.
  const { data: targets, error: targetsError } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(targetList),
  })
  const chats = (targets ?? []).filter(t => t.channel_type === TELEGRAM_CHANNEL && t.available)
  const { data: topics, error: topicsError } = useQuery({
    queryKey: ['channel-topics'],
    queryFn: () => api.channelTopics().then(targetList),
    enabled: chats.length > 0,
  })
  // No hand-off: the notice sits beside the job form's unsaved input, which a
  // hand-off would discard.
  const notice = (targetsError || topicsError) && (
    <ErrorNotice variant="inline" message={i18nT('components.jobForm.telegram_destination_load_failed')} />
  )
  if (chats.length === 0 && !value) return notice || null
  const rows = [
    ...chats.map(t => ({ id: t.target_id, label: i18nT('components.jobForm.telegram_private_chat', { id: t.target_id.slice(USER_PREFIX.length) }) })),
    ...(topics ?? []).map(t => ({ id: t.target_id, label: t.label })),
  ]
  // A saved destination the lists no longer carry is still shown, so opening the
  // form never reads as "off" for a job that is pinned.
  if (value && !rows.some(r => r.id === value)) rows.push({ id: value, label: value })
  const select = (
    <SimpleSelect
      options={rows.map(r => r.id)}
      optionLabels={rows.map(r => r.label)}
      value={value}
      onChange={onChange}
      clearLabel={i18nT('components.jobForm.telegram_destination_off')}
      aria-label={i18nT('components.jobForm.telegram_destination')}
    />
  )
  if (!labelled) return <>{select}{notice}</>
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.telegram_destination')}</span>
      <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.telegram_destination_hint')}</span>
      {select}
      {notice}
    </div>
  )
}
