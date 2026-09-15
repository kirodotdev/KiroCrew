import { useEffect, useId, useRef, useState } from 'react'
import { useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, Link2Off, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { useConnected } from '../hooks/useConnected'
import { useTranslation } from 'react-i18next'
import { useAppSelector, useAppStore } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { findReport } from '../utils/errorReport'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem } from './ui/dropdown-menu'
import { ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem } from './ui/context-menu'

type SourceLink = NonNullable<ChatSlot['source_links']>[number]
const unlinkKey = (slotKey: string) => ['unlink-source-link', slotKey]

/** Placeholder for the full-list query while it re-keys after a removal.
 *
 * Unlinking a preview chip patches the slot, which changes the query's
 * `signature` key; the refetch under the new key starts with no data. Without a
 * placeholder the entries fall back to the (now shorter) preview list until it
 * lands — an off-preview neighbour that has just been handed focus unmounts,
 * and focus drops to <body>. Carrying the previous list keeps every remaining
 * entry mounted (same `identity` keys) through the round trip. The previous
 * data was already pruned of the removed identity, so nothing stale shows.
 *
 * Only across the SAME slot AND the SAME session identity: a placeholder from
 * another slot's query would list that slot's links here for a frame, and — the
 * case that matters for a durable unlink — a same-KEY session that was RECREATED
 * while this menu is open (a new session reusing ``slotKey``) must NOT keep the
 * OLD session's links selectable, or unlinking one would durably dismiss a
 * shared identity from the REPLACEMENT session. Keying on the session identity
 * (``sessionId`` = server ``row_identity`` or, failing that, ``created``)
 * alongside the slot key drops the placeholder the instant the session behind
 * the key changes. Errors are unaffected — React Query drops the placeholder as
 * soon as the query leaves `pending`. */
export const sameSlotPlaceholder = (slotKey: string, sessionId: string) =>
  (previous: SourceLink[] | undefined, previousQuery: { queryKey: readonly unknown[] } | undefined): SourceLink[] | undefined =>
    previousQuery?.queryKey[0] === 'session-source-links'
    && previousQuery.queryKey[1] === slotKey
    && previousQuery.queryKey[2] === sessionId
      ? previous
      : undefined

/** The mutation cache shares pending removals with chips on every surface.
 * Rejection drops the pending mask, restoring chips without a snapshot rollback
 * that could overwrite a concurrent slots push. */
export function usePendingSourceUnlinks(slotKey: string) {
  return useMutationState({
    filters: { mutationKey: unlinkKey(slotKey), status: 'pending' },
    select: mutation => mutation.state.variables as string,
  })
}

/** One submenu in the EXISTING session menu, never another chip-strip trigger. */
export default function SourceLinksSubmenu({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const { t: i18nT } = useTranslation()
  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const [open, setOpen] = useState(false)
  // Per-item confirmation: the first select on a link STAGES it (the entry
  // switches to a "confirm unlink" affordance); only a second select on the
  // SAME staged link commits the DELETE. This makes the destructive, durable
  // removal a deliberate two-step action rather than firing on a single click,
  // and it is why the submenu header can describe permanence before any entry
  // is chosen — selecting a link never immediately unlinks it. Reset whenever
  // the menu closes so a stale staged item never carries into a later open.
  const [confirming, setConfirming] = useState<string | null>(null)
  // The session generation captured when the currently-staged unlink was staged.
  const stagedGeneration = useRef<string>('')
  const triggerRef = useRef<HTMLDivElement>(null)
  const errorId = useId()
  const connected = useConnected()
  const store = useAppStore()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  const links = slot?.source_links ?? []
  const total = slot?.source_links_total
  // Stable identity of the session CURRENTLY behind ``slotKey``. It must change
  // on a same-key session RECREATION so a staged confirm cannot commit against
  // the replacement. ``row_identity`` alone is insufficient: a purely-local
  // session's ``row_identity`` falls back to its slot key, which a recreated
  // session at the same key REUSES — so combine it with the per-session
  // ``created`` stamp, which is fresh on every recreation. Any change re-keys
  // the links query and, via the effect below, clears any staged confirm — so a
  // durable unlink can never land on a replacement session using stale links.
  const sessionId = `${slot?.row_identity ?? slotKey}|${slot?.created ?? ''}`
  const queryClient = useQueryClient()
  const pending = usePendingSourceUnlinks(slotKey)
  // Drop a staged confirm the moment the session behind the key changes: the
  // entry the user staged belongs to the OLD session and must not commit here.
  useEffect(() => { setConfirming(null) }, [sessionId])
  // Same key as the expanded chip strip: reuse its complete list, but never
  // fetch just because the parent menu mounted or an untruncated list opened.
  const signature = `${total ?? ''}|${links.map(link => link.url).join(' ')}`
  const allLinks = useQuery({
    queryKey: ['session-source-links', slotKey, sessionId, signature],
    queryFn: async () => {
      const payload = await api.chatSlotSourceLinks(slotKey)
      if (!Array.isArray(payload?.links)) throw new Error('malformed source-links response')
      return payload.links
    },
    enabled: open && connected && (total ?? 0) > links.length,
    placeholderData: sameSlotPlaceholder(slotKey, sessionId),
    retry: false,
    staleTime: 30_000,
  })
  // Generation of the session CURRENTLY behind ``slotKey``, read live from the
  // store. ``row_identity`` alone is insufficient — a local session's falls
  // back to the key, which a recreated same-key session reuses — so combine it
  // with the per-session ``created`` stamp, fresh on every recreation. The
  // commit refuses to fire if this changed since the unlink was staged, so an
  // unlink can never land on a same-key replacement session.
  const slotGeneration = (key: string): string => {
    const s = store.getState().dashboard.slots.find(x => x.key === key)
    return `${s?.row_identity ?? key}|${s?.created ?? ''}`
  }
  const unlink = useMutation({
    mutationKey: unlinkKey(slotKey),
    mutationFn: (identity: string) => api.unlinkSourceLink(slotKey, identity),
    onMutate: () => store.getState().dashboard.slots.find(x => x.key === slotKey),
    onSuccess: (_result, identity, before) => {
      // The entry is about to unmount. Sub content does not trap focus, so if
      // the user is still ON that entry (keyboard Enter, or pointer hover),
      // hand focus to a neighbour first, else to the parent menu, whose roving
      // group lands it on an entry so the arrow keys keep working.
      // Focus elsewhere (menu closed, another control) is left alone.
      const active = document.activeElement
      if (active instanceof HTMLElement && active.dataset.sourceIdentity === identity) {
        const entries = Array.from(active.parentElement?.querySelectorAll<HTMLElement>('[data-source-identity]') ?? [])
        const index = entries.indexOf(active)
        const next = entries[index + 1] ?? entries[index - 1] ?? triggerRef.current?.closest<HTMLElement>('[role="menu"]') ?? triggerRef.current
        next?.focus()
      }
      // Patch only source fields from the CURRENT slot, not a render snapshot.
      // The server may already have pushed the removal while DELETE was pending.
      const current = store.getState().dashboard.slots.find(x => x.key === slotKey)
      const stillInPreview = current?.source_links?.some(link => link.identity === identity)
      const unchangedPreview = current?.source_links === before?.source_links && current?.source_links_total === before?.source_links_total
      if (current?.source_links && (stillInPreview || unchangedPreview)) {
        store.dispatch(updateSlot({
          key: slotKey,
          source_links: current.source_links.filter(link => link.identity !== identity),
          source_links_total: Math.max(0, (current.source_links_total ?? current.source_links.length) - 1),
        }))
      }
      queryClient.setQueriesData<SourceLink[]>({ queryKey: ['session-source-links', slotKey] },
        previous => previous?.filter(link => link.identity !== identity))
      void queryClient.invalidateQueries({ queryKey: ['session-source-links', slotKey] })
    },
  })
  // Menu content unmounts on dismissal. Retain the latest mutation outcome in
  // React Query so closing during DELETE cannot discard a later failure.
  const outcomes = useMutationState({ filters: { mutationKey: unlinkKey(slotKey) }, select: mutation => mutation.state.error })
  const unlinkError = outcomes.at(-1)
  const error = unlinkError ?? allLinks.error
  const rawError = error instanceof Error ? error.message : String(error ?? '')
  const message = unlinkError
    ? i18nT('pages.chatSidebar.unlink_source_link_failed')
    : allLinks.error ? i18nT('pages.chatSidebar.source_links_expand_failed') : null
  const shown = allLinks.data ?? links
  if (!shown.some(link => link.identity) && !(total && total > links.length)) return null

  return (
    <Sub open={open} onOpenChange={next => { setOpen(next); if (!next) setConfirming(null) }}>
      <SubTrigger ref={triggerRef}>
        <Link2Off className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
        <span className="flex-1">{i18nT('pages.chatSidebar.source_link_actions')}</span>
        <ChevronRight className="lucide-inline shrink-0" aria-hidden="true" />
      </SubTrigger>
      <SubContent className="max-w-[min(360px,calc(100vw-2rem))]" onClick={event => event.stopPropagation()}>
        <p className="px-3 py-1.5 text-[12px] text-muted whitespace-normal">
          {i18nT('pages.chatSidebar.unlink_source_link')}
        </p>
        {!connected && <p className="px-3 py-1.5 text-[12px] text-muted">{i18nT('utils.offline.disabled_gateway_offline', { label: i18nT('pages.chatSidebar.source_link_actions') })}</p>}
        {allLinks.isFetching && <Loader2 className="lucide-inline animate-spin mx-3" aria-label={i18nT('pages.chatPage.loading')} />}
        {shown.filter(link => link.identity).map(link => {
          const staged = confirming === link.identity
          return (
            <Item
              key={link.identity}
              data-source-identity={link.identity}
              data-confirming={staged || undefined}
              title={link.url}
              disabled={!connected || pending.length > 0}
              className="text-danger focus:text-danger"
              onSelect={event => {
                // Keep the result/error and its keyboard-reachable hand-off in
                // Radix's anchored, collision-aware content on desktop and touch.
                event.preventDefault()
                if (!connected || pending.length > 0 || !link.identity) return
                // First select STAGES this link (and un-stages any other);
                // second select on the SAME staged link commits the removal.
                if (staged) {
                  // Refuse to commit if the session behind ``slotKey`` was
                  // recreated since staging (its generation changed): the entry
                  // the user staged belongs to the OLD session, and the DELETE
                  // resolves by slot name — firing it would durably dismiss the
                  // replacement session's matching link. Drop the stale confirm.
                  const committed = stagedGeneration.current === slotGeneration(slotKey)
                  setConfirming(null)
                  if (committed) unlink.mutate(link.identity)
                } else {
                  stagedGeneration.current = slotGeneration(slotKey)
                  setConfirming(link.identity)
                }
              }}
            >
              <span className="break-all">
                {staged
                  ? i18nT('pages.chatSidebar.unlink_source_link_confirm', { label: link.label ?? `#${link.number}` })
                  : <>{link.label ?? `#${link.number}`} · {link.repo || link.url}</>}
              </span>
            </Item>
          )
        })}
        {message && (
          <>
            <div className="px-3 py-1.5">
              <ErrorNotice id={errorId} message={message} report={findReport(rawError)} testId="session-source-unlink-error" />
            </div>
            <ErrorNoticeMenuItem Item={Item} message={rawError} describedBy={errorId} />
            {allLinks.error && <Item disabled={!connected || allLinks.isFetching} onSelect={event => { event.preventDefault(); void allLinks.refetch() }}>{i18nT('pages.chatSidebar.retry')}</Item>}
          </>
        )}
      </SubContent>
    </Sub>
  )
}
