import { useMutation, useQuery } from '@tanstack/react-query'
import { MessageSquareShare, Link2Off } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { api } from '../api/client'
import { DropdownMenuItem } from './ui/dropdown-menu'
import { ContextMenuItem } from './ui/context-menu'

import { i18nT } from '../i18n/t'
/**
 * Connected Slack link/unlink section for SessionActionsMenu — shared by the
 * header dropdown and BOTH sidebar row menus (dropdown + right-click). Keyed on
 * `slotKey`: it reads the live `slack_linked` flag from the store and drives the
 * writes through `useMutation` (the package's standard server-write pattern, cf.
 * `pinMutation` in useSessionActions) — `linkMutation` dispatches the optimistic
 * `updateSlot` on success, `unlinkMutation` clears the link on success, and the
 * `isPending` flags gate double-submits (the guard the old header wrapper had).
 * The workspace channel list comes from the shared ['slack-channels'] query,
 * which dedupes across every menu instance and runs only while a menu is open.
 * Renders real menu Items matched to the enclosing surface `variant`, so Radix
 * auto-closes the menu on select.
 *
 * Visibility mirrors the prior header contract exactly:
 *   • linked                      → "Post reminder in Slack" + "Unlink from Slack"
 *   • unlinked & channels loaded  → "Send to Slack" + one item per channel
 *   • unlinked & channels missing → nothing (returns null; its separator collapses)
 */
export default function SlackLinkSection({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const dispatch = useAppDispatch()
  const linked = useAppSelector(s => !!s.dashboard.slots.find(x => x.key === slotKey)?.slack_linked)

  // Global workspace resource — same key the header used to fetch eagerly, now
  // fetched lazily on menu-open and shared across every menu instance.
  const { data: channels } = useQuery({
    queryKey: ['slack-channels'],
    queryFn: () => api.slackChannels().then(c => (Array.isArray(c) ? (c as { id: string; name: string }[]) : null)),
  })

  // Link (optionally to a specific channel) — also used to re-post the reminder
  // when already linked (channel undefined). Optimistic updateSlot on success.
  const linkMutation = useMutation({
    mutationFn: (channel: string | undefined) => api.slackLink(slotKey, channel),
    onSuccess: (r) => {
      if (r?.ok) dispatch(updateSlot({ key: slotKey, slack_linked: true, slack_channel: r.channel, slack_thread_ts: r.thread_ts }))
    },
    onError: (e) => { console.warn('slackLink failed', e) },
  })
  const unlinkMutation = useMutation({
    mutationFn: () => api.unlinkSlack(slotKey),
    onSuccess: () => dispatch(updateSlot({ key: slotKey, slack_linked: false, slack_channel: undefined, slack_thread_ts: undefined })),
    onError: (e) => { console.warn('unlinkSlack failed; session stays linked', e) },
  })
  const link = (channel?: string) => { if (!linkMutation.isPending) linkMutation.mutate(channel) }
  const unlink = () => { if (!unlinkMutation.isPending) unlinkMutation.mutate() }

  // Linked branch does not depend on the channel list (matches the prior header).
  if (linked) {
    return (
      <>
        <Item className="text-ok focus:text-ok" onSelect={() => link()}>
          <MessageSquareShare size={13} className="shrink-0" /> {i18nT('components.slackLinkSection.post_reminder_in_slack')}
        </Item>
        <Item className="text-danger focus:text-danger" onSelect={unlink}>
          <Link2Off size={13} className="shrink-0" /> {i18nT('components.slackLinkSection.unlink_from_slack')}
        </Item>
      </>
    )
  }
  // Unlinked: only offer "Send to Slack" once the channel list is available.
  if (channels == null) return null
  return (
    <>
      <Item onSelect={() => link()}>
        <MessageSquareShare size={13} className="shrink-0 text-muted" /> {i18nT('components.slackLinkSection.send_to_slack')}
      </Item>
      {channels.filter(c => c.id !== 'dm').map(ch => (
        <Item key={ch.id} onSelect={() => link(ch.id)}># {ch.name}</Item>
      ))}
    </>
  )
}
