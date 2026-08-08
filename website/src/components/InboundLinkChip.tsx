import { useMutation } from '@tanstack/react-query'
import { ArrowLeftRight, Link2Off } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch, useAppSelector } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { addNotification } from '../store/notificationsSlice'
import { ChannelBrandIcon } from './ChannelBrandIcon'

/**
 * Header chip for a session that is being DRIVEN from another channel.
 *
 * A `direction: 'both'` link (created by an in-channel `!sessions` pick) makes
 * this session two-way: what you type here is also delivered to that channel,
 * and messages sent there arrive here. That side effect is otherwise invisible
 * — the session looks like any other dashboard tab — so it gets a persistent
 * chip rather than living only in a menu the user has to open. `origin` and
 * one-way `out` links deliberately render nothing here: they carry no surprise.
 *
 * Release is the dashboard-side equivalent of the channel's `!unlink`.
 */
export default function InboundLinkChip({ slotKey }: { slotKey?: string }) {
  const dispatch = useAppDispatch()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const inbound = slot?.links?.find(link => link.direction === 'both')

  const releaseMutation = useMutation({
    // Name the channel the chip labels: the unnamed form clears EVERY binding, so
    // releasing the Discord this chip names would also delete a Telegram binding
    // the user never mentioned — and its `accepts_inbound` with it.
    mutationFn: () => api.unlinkMirror(slotKey as string, inbound?.channel ?? ''),
    onSuccess: () => {
      dispatch(updateSlot({
        key: slotKey as string,
        links: (slot?.links ?? []).filter(link => link !== inbound),
      }))
      dispatch(addNotification({
        ts: String(Date.now()),
        title: i18nT('components.inboundLinkChip.released', { label: inbound?.label ?? '' }),
        body: '',
        kind: 'success',
      }))
    },
    onError: (e) => {
      dispatch(addNotification({
        ts: String(Date.now()),
        title: i18nT('components.inboundLinkChip.release_failed', {
          reason: e instanceof Error && e.message ? e.message : 'unknown error',
        }),
        body: '',
        kind: 'error',
      }))
    },
  })

  if (!slotKey || !inbound) return null

  const release = () => {
    if (releaseMutation.isPending) return
    const prompt = i18nT('components.inboundLinkChip.confirm_release', { label: inbound.label })
    if (!window.confirm(prompt)) return
    releaseMutation.mutate()
  }

  return (
    <span
      className="pointer-events-auto inline-flex items-center gap-1.5 rounded-md border border-border bg-accent-subtle px-2 py-0.5 text-[11px] text-muted"
      title={i18nT('components.inboundLinkChip.tooltip', { label: inbound.label })}
    >
      <ArrowLeftRight size={11} className="shrink-0 text-accent" aria-hidden />
      <ChannelBrandIcon channel={inbound.channel} size={11} />
      <span className="truncate max-w-[22ch]">
        {i18nT('components.inboundLinkChip.driven_from', { label: inbound.label })}
      </span>
      <button
        type="button"
        className="inline-flex items-center gap-0.5 cursor-pointer border-none bg-transparent p-0 text-[11px] text-muted hover:text-danger transition-colors disabled:opacity-50"
        onClick={release}
        disabled={releaseMutation.isPending}
        aria-label={i18nT('components.inboundLinkChip.release_aria', { label: inbound.label })}
      >
        <Link2Off size={11} className="shrink-0" aria-hidden />
        {i18nT('components.inboundLinkChip.release')}
      </button>
    </span>
  )
}
