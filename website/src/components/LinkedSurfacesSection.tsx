import { Fragment } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link2Off, MessageSquareShare } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch, useAppSelector } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import type { SessionLink } from '../types'
import { ChannelBrandIcon } from './ChannelBrandIcon'
import { ContextMenuItem } from './ui/context-menu'
import { DropdownMenuItem } from './ui/dropdown-menu'

function ConnectedBadge({ link }: { link: SessionLink }) {
  return (
    <div role="status" className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted">
      <ChannelBrandIcon channel={link.channel} size={13} />
      <span className="min-w-0 truncate">
        {i18nT('components.linkedSurfacesSection.connected', { label: link.label })}
      </span>
      <span className="ml-auto shrink-0 rounded bg-bg-hover px-1.5 py-0.5 text-[10px]">
        {link.direction === 'origin'
          ? i18nT('components.linkedSurfacesSection.origin')
          : i18nT('components.linkedSurfacesSection.mirror')}
      </span>
    </div>
  )
}

/** Channel-neutral link actions shared by every session menu surface. */
export default function LinkedSurfacesSection({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const dispatch = useAppDispatch()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const wireLinks = slot?.links ?? []
  const links: SessionLink[] = slot?.slack_linked && !wireLinks.some(link => link.channel === 'slack')
    ? [...wireLinks, { channel: 'slack', label: 'Slack', target: '', direction: 'out', live: true }]
    : wireLinks
  const slackLink = links.find(link => link.channel === 'slack')
  const nonSlackLinks = links.filter(link => link.channel !== 'slack')

  const { data: channels } = useQuery({
    queryKey: ['slack-channels'],
    queryFn: () => api.slackChannels().then(c => (Array.isArray(c) ? (c as { id: string; name: string }[]) : null)),
  })

  const slackLinkMutation = useMutation({
    mutationFn: (channel: string | undefined) => api.slackLink(slotKey, channel),
    onSuccess: (r) => {
      if (r?.ok) dispatch(updateSlot({ key: slotKey, slack_linked: true, slack_channel: r.channel, slack_thread_ts: r.thread_ts }))
    },
    onError: (e) => { console.warn('slackLink failed', e) },
  })
  const slackUnlinkMutation = useMutation({
    mutationFn: () => api.unlinkSlack(slotKey),
    onSuccess: () => dispatch(updateSlot({
      key: slotKey,
      links: links.filter(link => link.channel !== 'slack'),
      slack_linked: false,
      slack_channel: undefined,
      slack_thread_ts: undefined,
    })),
    onError: (e) => { console.warn('unlinkSlack failed; session stays linked', e) },
  })
  const mirrorReminderMutation = useMutation({
    mutationFn: () => api.remindMirror(slotKey),
    onError: (e) => { console.warn('mirror reminder failed', e) },
  })
  const mirrorUnlinkMutation = useMutation({
    mutationFn: (link: SessionLink) => api.unlinkMirror(slotKey).then(result => ({ link, result })),
    onSuccess: ({ link }) => dispatch(updateSlot({
      key: slotKey,
      links: links.filter(candidate => candidate !== link),
    })),
    onError: (e) => { console.warn('mirror unlink failed; session stays linked', e) },
  })

  const linkSlack = (channel?: string) => {
    if (!slackLinkMutation.isPending) slackLinkMutation.mutate(channel)
  }
  const unlinkSlack = () => {
    if (!slackUnlinkMutation.isPending) slackUnlinkMutation.mutate()
  }
  const remindMirror = () => {
    if (!mirrorReminderMutation.isPending) mirrorReminderMutation.mutate()
  }
  const unlinkMirror = (link: SessionLink) => {
    if (!mirrorUnlinkMutation.isPending) mirrorUnlinkMutation.mutate(link)
  }

  return (
    <>
      {links.map(link => (
        <ConnectedBadge key={`${link.channel}:${link.direction}:${link.target}`} link={link} />
      ))}

      {nonSlackLinks.map(link => link.direction === 'out' ? (
        <Fragment key={`actions:${link.channel}:${link.target}`}>
          {link.live && (
            <Item className="text-ok focus:text-ok" onSelect={remindMirror}>
              <MessageSquareShare size={13} className="shrink-0" />
              {i18nT('components.linkedSurfacesSection.post_reminder', { label: link.label })}
            </Item>
          )}
          <Item className="text-danger focus:text-danger" onSelect={() => unlinkMirror(link)}>
            <Link2Off size={13} className="shrink-0" />
            {i18nT('components.linkedSurfacesSection.stop_mirroring', { label: link.label })}
          </Item>
        </Fragment>
      ) : null)}

      {slackLink ? (
        <>
          <Item className="text-ok focus:text-ok" onSelect={() => linkSlack()}>
            <MessageSquareShare size={13} className="shrink-0" /> {i18nT('components.slackLinkSection.post_reminder_in_slack')}
          </Item>
          <Item className="text-danger focus:text-danger" onSelect={unlinkSlack}>
            <Link2Off size={13} className="shrink-0" /> {i18nT('components.slackLinkSection.unlink_from_slack')}
          </Item>
        </>
      ) : nonSlackLinks.length === 0 && channels != null ? (
        <>
          <Item onSelect={() => linkSlack()}>
            <MessageSquareShare size={13} className="shrink-0 text-muted" /> {i18nT('components.slackLinkSection.send_to_slack')}
          </Item>
          {channels.filter(c => c.id !== 'dm').map(ch => (
            <Item key={ch.id} onSelect={() => linkSlack(ch.id)}># {ch.name}</Item>
          ))}
        </>
      ) : null}
    </>
  )
}
