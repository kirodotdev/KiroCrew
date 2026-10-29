/** Settings > People > Who can open this Crew.
 *
 *  This card is shown only when the server confirms local authorization is on.
 *  The access set is computed, never typed: the owner, accepted invites,
 *  carried-over entries and people the owner approves here. Other reachable
 *  people wait for approval. With local authorization off, Tailscale access
 *  rules decide and this card is hidden.
 *
 *  The first invite or approve, while nobody else has access, asks once per Crew
 *  (`useFirstShareGate`): who gets in, that they will see every session and Crew
 *  memory, with real counts, that a laptop host loses them when it sleeps, and
 *  where sharing stops.
 *
 *  The card is owner-only: the read answers 403 to anyone else and the card
 *  renders nothing. */
import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, UserMinus, X } from 'lucide-react'
import { api } from '../../api/client'
import { inviteErrorCode } from '../../api/sharedCrewInvites'
import { initials, safeAvatarUrl, type AccessView, type ApprovedPerson, type FirstShare, type WaitingPerson } from '../../api/sharedCrewAccess'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { useGuardedMutation } from '../../hooks/sharedCrew/useGuardedMutation'
import { fmtDateTime, fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

export const sharedCrewAccessKey = [...sharedCrewKeys.all, 'access'] as const

export function useSharedCrewAccess() {
  return useQuery({
    queryKey: sharedCrewAccessKey,
    queryFn: () => api.sharedCrewAccess(),
    retry: false,
    // Someone may be waiting right now; re-read while the page is open.
    refetchInterval: 30_000,
  })
}

function accessErrorText(code: string): string {
  switch (code) {
    case 'first_share_ack_required': return i18nT('sharedCrew.waitlist_err_ack')
    case 'not_waiting': return i18nT('sharedCrew.waitlist_err_gone')
    case 'owner_only': return i18nT('sharedCrew.invite_err_owner')
    default: return i18nT('sharedCrew.waitlist_err_generic')
  }
}

/** Who gets in, what they will see and where sharing stops, as the warning's body.
 *  With local authorization on, Crew admits only invited and approved people;
 *  off, anyone Tailscale access rules let reach this machine gets in. */
function FirstShareBody({ share, localAuthorization }: { share: FirstShare; localAuthorization: boolean }) {
  const counted = share.sessions !== null && share.memory_items !== null
  const laptop = share.host.laptop
  return (
    <div className="flex flex-col gap-2" data-testid="first-share-body">
      <p className="m-0" data-testid="first-share-audience">
        {localAuthorization ? i18nT('sharedCrew.first_share_audience_local') : i18nT('sharedCrew.first_share_audience_tailscale')}
      </p>
      <p className="m-0">
        {counted
          ? i18nT('sharedCrew.first_share_body', { sessions: fmtNumber(share.sessions ?? 0), memory: fmtNumber(share.memory_items ?? 0) })
          : i18nT('sharedCrew.first_share_body_uncounted')}
      </p>
      {laptop && (
        <p className="m-0 text-muted" data-testid="first-share-laptop">
          {share.host.state === 'available' ? i18nT('sharedCrew.first_share_laptop') : i18nT('sharedCrew.first_share_laptop_no_keep_awake')}
        </p>
      )}
      <p className="m-0" data-testid="first-share-stop">
        {localAuthorization ? i18nT('sharedCrew.first_share_stop_local') : i18nT('sharedCrew.first_share_stop_tailscale')}
      </p>
    </div>
  )
}

/** Allow sharing when no warning is due, or after the owner confirms it. */
export function useFirstShareGate() {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const confirmShare = useCallback(async () => {
    // A fresh read at the moment of sharing: the warning is due only while
    // nobody else has access. TailscaleAccess reports a failed read from this
    // shared query; the action still lets the server decide whether it needs
    // the first-share acknowledgment.
    let view: AccessView | null = null
    try {
      view = await qc.fetchQuery({ queryKey: sharedCrewAccessKey, queryFn: () => api.sharedCrewAccess(), staleTime: 0 })
    } catch {
      view = null
    }
    const share = view?.first_share
    if (!share?.needs_ack) return true
    const ok = await confirm({
      title: i18nT('sharedCrew.first_share_title'),
      body: <FirstShareBody share={share} localAuthorization={view?.local_authorization === true} />,
      confirmLabel: i18nT('sharedCrew.first_share_confirm'),
      danger: false,
      primary: true,
    })
    if (!ok) return false
    try {
      await api.sharedCrewAckFirstShare()
    } catch {
      // The action still runs: the server refuses it unacknowledged, and the
      // action's own error notice shows that refusal.
    }
    await qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    return true
  }, [confirm, qc])
  return { confirmShare, firstShareDialog: confirmDialog }
}

function Avatar({ name, url }: { name: string; url: string }) {
  const src = safeAvatarUrl(url)
  return src ? (
    <img src={src} alt="" referrerPolicy="no-referrer" className="w-8 h-8 rounded-full object-cover shrink-0" />
  ) : (
    <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
      {initials(name)}
    </span>
  )
}

function WaitingRow({ person, confirmShare }: { person: WaitingPerson; confirmShare: () => Promise<boolean> }) {
  const qc = useQueryClient()
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
  }
  const approve = useMutation({ mutationFn: () => api.sharedCrewApprove(person.user_id), onSuccess: refresh })
  const approval = useGuardedMutation(approve.mutateAsync, confirmShare)
  const dismiss = useMutation({ mutationFn: () => api.sharedCrewDismiss(person.user_id), onSuccess: refresh })
  const name = person.display_name || person.login
  const err = approve.isError ? inviteErrorCode(approve.error) || 'error' : dismiss.isError ? inviteErrorCode(dismiss.error) || 'error' : ''
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`waiting-${person.user_id}`}>
      <Avatar name={name} url={person.avatar_url} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
        {name !== person.login && <span className="text-[12px] text-muted truncate" title={person.login}>{person.login}</span>}
        <span className="text-[12px] text-muted">{i18nT('sharedCrew.waitlist_first_seen', { when: fmtDateTime(person.first_seen * 1000) })}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        <Btn type="button" primary onClick={() => { void approval.run() }} disabled={approval.isPending} data-testid="waiting-approve">
          <Check size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_approve')}
        </Btn>
        <Btn type="button" onClick={() => dismiss.mutate()} disabled={dismiss.isPending} data-testid="waiting-dismiss">
          <X size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_dismiss')}
        </Btn>
      </div>
      {/* No hand-off: the Invites card on this page can hold an unsaved invite or API-key draft. */}
      {err && <ErrorNotice variant="inline" className="w-full" message={accessErrorText(err)} testId="waiting-error" />}
    </li>
  )
}

function ApprovedRow({ person }: { person: ApprovedPerson }) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const remove = useMutation({
    mutationFn: () => api.sharedCrewRemoveApproved(person.user_id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
      void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
    },
  })
  const name = person.display_name || person.login
  const removal = useGuardedMutation(remove.mutateAsync, () => confirm({
    title: i18nT('sharedCrew.waitlist_remove_title', { name }),
    body: i18nT('sharedCrew.waitlist_remove_body'),
    confirmLabel: i18nT('sharedCrew.waitlist_remove_confirm'),
  }))
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`approved-${person.user_id}`}>
      <Avatar name={name} url={person.avatar_url} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          <Badge variant="ok">{i18nT('sharedCrew.waitlist_approved_badge')}</Badge>
        </div>
        {name !== person.login && <span className="text-[12px] text-muted truncate" title={person.login}>{person.login}</span>}
        <span className="text-[12px] text-muted">{i18nT('sharedCrew.waitlist_approved_at', { when: fmtDateTime(person.approved_at * 1000) })}</span>
      </div>
      <Btn type="button" danger onClick={() => { void removal.run() }} disabled={removal.isPending} data-testid="approved-remove">
        <UserMinus size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.waitlist_remove')}
      </Btn>
      {/* No hand-off: the Invites card on this page can hold an unsaved invite or API-key draft. */}
      {remove.isError && <ErrorNotice variant="inline" className="w-full" message={accessErrorText(inviteErrorCode(remove.error))} testId="approved-remove-error" />}
      {confirmDialog}
    </li>
  )
}

/** The whole card. Hidden for anyone but the owner, or a gateway without the routes (404). */
export function TailscaleAccess() {
  const access = useSharedCrewAccess()
  const { confirmShare, firstShareDialog } = useFirstShareGate()
  if (access.isLoading) return null
  const view = access.data
  const code = access.isError ? inviteErrorCode(access.error) : ''
  // Missing routes and an initial owner-only refusal intentionally hide the card.
  if (!view && (!access.isError || code === 'owner_only')) return null
  // Tailscale alone decides who gets in, but a failed refresh is still reported.
  if (view?.local_authorization !== true && !access.isError) return null
  return (
    <SettingsSection title={i18nT('sharedCrew.waitlist_title')}>
      {access.isError && (
        <div>
          {/* No hand-off: the Invites card on this page can hold an unsaved invite or API-key draft. */}
          <ErrorNotice message={accessErrorText(code)} testId="access-load-error" />
          <Btn onClick={() => { void access.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      )}
      {view?.local_authorization === true && <SettingsCard>
        <div className="flex flex-col gap-3 min-w-0" data-testid="tailscale-access">
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.waitlist_body')}</p>
          {/* No hand-off: the Invites card on this page can hold an unsaved invite or API-key draft. */}
          {!view.readable && (
            <ErrorNotice variant="inline" message={i18nT('sharedCrew.waitlist_unreadable')} testId="access-unreadable" />
          )}
          <div className="flex flex-col gap-1 min-w-0">
            <h4 className="text-[13px] font-semibold text-text-strong m-0">{i18nT('sharedCrew.waitlist_waiting_title')}</h4>
            {view.waiting.length === 0 ? (
              <p className="text-[13px] text-muted" data-testid="waiting-empty">{i18nT('sharedCrew.waitlist_waiting_empty')}</p>
            ) : (
              <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.waitlist_waiting_title')} data-testid="waiting-list">
                {view.waiting.map(p => <WaitingRow key={p.user_id} person={p} confirmShare={confirmShare} />)}
              </ul>
            )}
          </div>
          {view.approved.length > 0 && (
            <div className="flex flex-col gap-1 min-w-0 pt-2 border-t border-border">
              <h4 className="text-[13px] font-semibold text-text-strong m-0">{i18nT('sharedCrew.waitlist_approved_title')}</h4>
              <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.waitlist_approved_title')} data-testid="approved-list">
                {view.approved.map(p => <ApprovedRow key={p.user_id} person={p} />)}
              </ul>
            </div>
          )}
          {view.seeded.length > 0 && (
            <p className="text-[12px] text-muted break-words" data-testid="access-seeded">{i18nT('sharedCrew.waitlist_seeded', { entries: fmtList(view.seeded) })}</p>
          )}
        </div>
        {firstShareDialog}
      </SettingsCard>}
    </SettingsSection>
  )
}
