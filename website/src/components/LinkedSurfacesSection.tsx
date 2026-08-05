import { Fragment } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link2Off, MessageSquareShare } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch, useAppSelector } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { addNotification } from '../store/notificationsSlice'
import type { ConfiguredChannelTarget, SessionLink } from '../types'
import { ChannelBrandIcon } from './ChannelBrandIcon'
import { ContextMenuItem } from './ui/context-menu'
import { DropdownMenuItem } from './ui/dropdown-menu'

function ConnectedBadge({ link }: { link: SessionLink }) {
  // `live: false` means the channel's transport is absent or cannot send
  // proactively, so the reminder action is hidden — without reflecting that
  // here the row still reads "Connected" and the action simply vanishes with no
  // explanation. `origin` links are read-only by nature and never carried an
  // action, so liveness is only meaningful for a mirror.
  const offline = link.direction !== 'origin' && !link.live
  // Role + liveness, not one OR the other: replacing the role with "Offline"
  // erases WHICH kind of dead link the user is about to release.
  const role = link.direction === 'origin'
    ? i18nT('components.linkedSurfacesSection.origin')
    : link.direction === 'both'
      ? i18nT('components.linkedSurfacesSection.two_way')
      : i18nT('components.linkedSurfacesSection.mirror')
  return (
    <div role="status" className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted">
      <ChannelBrandIcon channel={link.channel} size={13} />
      <span className="min-w-0 truncate">
        {i18nT('components.linkedSurfacesSection.connected', { label: link.label })}
      </span>
      <span className="ml-auto shrink-0 rounded bg-bg-hover px-1.5 py-0.5 text-[10px]">
        {offline
          ? `${role} · ${i18nT('components.linkedSurfacesSection.offline')}`
          : role}
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

  const { data: targets } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
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
  const mirrorLinkMutation = useMutation({
    mutationFn: (target: ConfiguredChannelTarget) => api.linkMirror(
      slotKey,
      target.channel_type,
      target.target_id,
    ).then(result => ({ target, result })),
    onSuccess: ({ target, result }) => {
      if (!result?.ok) return
      dispatch(updateSlot({
        key: slotKey,
        links: [
          ...links.filter(link => link.direction === 'origin'),
          {
            channel: target.channel_type,
            label: target.label,
            target: result.conversation_id || target.target_id,
            direction: 'out',
            live: true,
          },
        ],
      }))
    },
    onError: (e) => notify(
      'error',
      i18nT('components.linkedSurfacesSection.link_failed', {
        reason: e instanceof Error && e.message ? e.message : 'unknown error',
      }),
    ),
  })
  // The reminder fires into ANOTHER app the user may not be watching, and the
  // Radix menu closes on select — so with no feedback a delivery that never
  // happened is indistinguishable from one that did. The backend distinguishes
  // "mirror channel is not live" (503) from "failed to post reminder" (502) and
  // `j()` throws ApiError for both, so surface each outcome in the notification
  // feed (same client-side path DevFleetPage uses).
  const notify = (kind: 'success' | 'error', title: string) => {
    dispatch(addNotification({ ts: String(Date.now()), title, body: '', kind }))
  }
  const mirrorReminderMutation = useMutation({
    mutationFn: (link: SessionLink) => api.remindMirror(slotKey).then(result => ({ link, result })),
    onSuccess: ({ link }) => notify(
      'success',
      i18nT('components.linkedSurfacesSection.reminder_sent', { label: link.label }),
    ),
    onError: (e) => notify(
      'error',
      i18nT('components.linkedSurfacesSection.reminder_failed', {
        reason: e instanceof Error && e.message ? e.message : 'unknown error',
      }),
    ),
  })
  const mirrorUnlinkMutation = useMutation({
    mutationFn: (link: SessionLink) => api.unlinkMirror(slotKey).then(result => ({ link, result })),
    onSuccess: ({ link }) => {
      dispatch(updateSlot({
        key: slotKey,
        links: links.filter(candidate => candidate !== link),
      }))
      // Branch the closure message the same way the item label and confirm do:
      // a two-way binding is RELEASED, and saying "stopped mirroring" here
      // contradicts the wording the user just clicked and confirmed.
      notify('success', link.direction === 'both'
        ? i18nT('components.linkedSurfacesSection.released', { label: link.label })
        : i18nT('components.linkedSurfacesSection.mirror_stopped', { label: link.label }))
    },
    onError: (e) => notify(
      'error',
      i18nT('components.linkedSurfacesSection.stop_failed', {
        reason: e instanceof Error && e.message ? e.message : 'unknown error',
      }),
    ),
  })

  const linkSlack = (channel?: string) => {
    if (!slackLinkMutation.isPending) slackLinkMutation.mutate(channel)
  }
  const linkTarget = (target: ConfiguredChannelTarget) => {
    if (target.channel_type === 'slack') {
      linkSlack(target.target_id)
    } else if (target.available && !mirrorLinkMutation.isPending) {
      mirrorLinkMutation.mutate(target)
    }
  }
  const unlinkSlack = () => {
    if (!slackUnlinkMutation.isPending) slackUnlinkMutation.mutate()
  }
  const remindMirror = (link: SessionLink) => {
    if (!mirrorReminderMutation.isPending) mirrorReminderMutation.mutate(link)
  }
  // Confirm before stopping: this is one click and sits at identical weight
  // directly under the safe "Post reminder". Re-linking is available from the
  // configured-target picker, but stopping still severs live delivery and
  // should not happen on a menu misclick. Matches the window.confirm precedent
  // for destructive actions elsewhere (HooksPage, ArtifactsPage,
  // WebAppArtifactCard).
  const unlinkMirror = (link: SessionLink) => {
    if (mirrorUnlinkMutation.isPending) return
    const prompt = link.direction === 'both'
      ? i18nT('components.linkedSurfacesSection.confirm_release', { label: link.label })
      : i18nT('components.linkedSurfacesSection.confirm_stop_mirroring', { label: link.label })
    if (!window.confirm(prompt)) return
    mirrorUnlinkMutation.mutate(link)
  }

  return (
    <>
      {links.map(link => (
        <ConnectedBadge key={`${link.channel}:${link.direction}:${link.target}`} link={link} />
      ))}

      {nonSlackLinks.map(link => link.direction !== 'origin' ? (
        <Fragment key={`actions:${link.channel}:${link.target}`}>
          {link.live && (
            <Item className="text-ok focus:text-ok" onSelect={() => remindMirror(link)}>
              <MessageSquareShare size={13} className="shrink-0" />
              {i18nT('components.linkedSurfacesSection.post_reminder', { label: link.label })}
            </Item>
          )}
          {/* A two-way binding is RELEASED, not "stopped mirroring": the user is
           *  detaching a resumed session, and the same wording as a one-way
           *  mirror would understate that messages from that channel currently
           *  land in this session. Both route through the same confirm. */}
          <Item className="text-danger focus:text-danger" onSelect={() => unlinkMirror(link)}>
            <Link2Off size={13} className="shrink-0" />
            {link.direction === 'both'
              ? i18nT('components.linkedSurfacesSection.release', { label: link.label })
              : i18nT('components.linkedSurfacesSection.stop_mirroring', { label: link.label })}
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
      ) : nonSlackLinks.length === 0 && targets != null ? (
        <>
          {targets.map(target => (
            <Item
              key={`${target.channel_type}:${target.target_id}`}
              aria-disabled={!target.available}
              className={!target.available ? 'opacity-60' : undefined}
              onSelect={(event) => {
                if (!target.available) {
                  event.preventDefault()
                  notify(
                    'error',
                    target.unavailable_reason
                      || i18nT('components.linkedSurfacesSection.unavailable'),
                  )
                  return
                }
                linkTarget(target)
              }}
            >
              <ChannelBrandIcon channel={target.channel_type} size={13} />
              <span className="flex min-w-0 flex-col">
                <span className="truncate">
                  {target.channel_type === 'slack' && target.target_id === 'dm'
                    ? i18nT('components.slackLinkSection.send_to_slack')
                    : target.label}
                </span>
                {!target.available && (
                  <span className="truncate text-[11px] text-muted">
                    {target.unavailable_reason
                      || i18nT('components.linkedSurfacesSection.unavailable')}
                  </span>
                )}
              </span>
            </Item>
          ))}
        </>
      ) : null}
    </>
  )
}
