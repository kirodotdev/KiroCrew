/** Settings > People > Invite.
 *
 *  The one invite flow, entered from the one `Invite…` button (`InviteEntry`):
 *  type teammates' emails (or make a link), optionally
 *  give them a name and a picture, and send. Tailscale mails the share invite;
 *  when the teammate accepts, Crew learns their real Tailscale login and lets
 *  that login in. The name and picture are display only: who gets in is always
 *  the login Tailscale reports, even when they accepted from another account.
 *
 *  Without a connected Tailscale API key the dialog links the admin console's
 *  Machines page, where the owner shares this machine by hand. The key is sent
 *  once and never shown again.
 *
 *  After an invite is made, `CopyInviteMessage` copies the message to send with
 *  it. An invite nobody has accepted yet is a Waiting row in the People list
 *  (`InviteRow`). The first invite, while nobody else has access, asks once per
 *  Crew (`useFirstShareGate`) with one warning line: they will see every session
 *  and Crew memory, with real counts. One acknowledgement covers both transports.
 *
 *  Tailscale and AWS SSM are tabs of the one dialog, shown only when both are
 *  on; the SSM tab and its rows live in `SsmInvite.tsx`. */
import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, Link2, Mail, RefreshCw, Trash2, UserPlus } from 'lucide-react'
import { api } from '../../api/client'
import {
  AVATAR_MAX_BYTES,
  AVATAR_TYPES,
  TAILSCALE_ADMIN_KEYS_URL,
  TAILSCALE_ADMIN_MACHINES_URL,
  avatarUrl,
  fileToBase64,
  inviteErrorCode,
  looksLikeApiKey,
  looksLikeEmail,
  parseEmails,
  type FirstShare,
  type FirstShareView,
  type InviteRecord,
  type TailscaleInvite,
  type TailscaleInvitesStatus,
} from '../../api/sharedCrewInvites'
import { heldLinks, invitesPinnedOff } from '../../api/sharedCrewUi'
import { useSharedCrewCapabilities, useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { ssmStatusKey } from '../../api/sharedCrewSsm'
import { ssmConfigured, SsmReadError, SsmTab, useSsmStatus } from './SsmInvite'
import { tailnetDirectActive } from '../../components/TailnetDirectLink'
import { buildInviteMessage, ownerDisplayName } from './inviteMessage'
import { Badge, Btn, Input, Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { copyToClipboard } from '../../utils/clipboard'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { useGuardedMutation } from '../../hooks/sharedCrew/useGuardedMutation'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useConfirm } from '../../components/ConfirmDialog'

export const tailscaleInvitesKey = [...sharedCrewKeys.all, 'tailscale-invites'] as const
export const sharedCrewAccessKey = [...sharedCrewKeys.all, 'access'] as const

/** The transports an invite can go over. */
export type InviteTransport = 'tailscale' | 'ssm'

/** Where to stop sharing, in the words of the path that shared the Crew. A new
 *  sharing path adds its case with its own catalog key. */
function stopSharingText(transport: InviteTransport): string {
  switch (transport) {
    case 'ssm': return i18nT('sharedCrew.first_share_stop_ssm')
    default: return i18nT('sharedCrew.first_share_stop_tailscale')
  }
}

/** Who can open the Crew, in the words of the path that shares it. */
function audienceText(transport: InviteTransport): string {
  switch (transport) {
    case 'ssm': return i18nT('sharedCrew.first_share_audience_ssm')
    default: return i18nT('sharedCrew.first_share_audience_tailscale')
  }
}

/** Who can open the Crew, what they will see, the laptop note and where to stop sharing. */
function FirstShareBody({ share, transport }: { share: FirstShare; transport: InviteTransport }) {
  const counted = share.sessions !== null && share.memory_items !== null
  return (
    <div className="flex flex-col gap-2" data-testid="first-share-body">
      <p className="m-0" data-testid="first-share-audience">{audienceText(transport)}</p>
      <p className="m-0">
        {counted
          ? i18nT('sharedCrew.first_share_body', { sessions: fmtNumber(share.sessions ?? 0), memory: fmtNumber(share.memory_items ?? 0) })
          : i18nT('sharedCrew.first_share_body_uncounted')}
      </p>
      {share.host.laptop && (
        <p className="m-0 text-muted" data-testid="first-share-laptop">
          {share.host.state === 'available' ? i18nT('sharedCrew.first_share_laptop') : i18nT('sharedCrew.first_share_laptop_no_keep_awake')}
        </p>
      )}
      <p className="m-0" data-testid="first-share-stop">{stopSharingText(transport)}</p>
    </div>
  )
}

/** Copies the message the owner sends with an invite: who invited them, how to
 *  accept, and the Crew links. Renders nothing until the Crew has a link to
 *  send. `inviteUrl` is a link invite's URL; without it the email line is used.
 *  `message` is a finished message (the AWS SSM invite), copied as it is: the
 *  button reads and behaves the same for every path. */
export function CopyInviteMessage({ inviteUrl = '', testId = 'invite-copy-message', primary = false, message, onCopied }: { inviteUrl?: string; testId?: string; primary?: boolean; message?: string; onCopied?: () => void }) {
  const caps = useSharedCrewCapabilities({ fetchOnMount: false })
  const people = useSharedCrewPeople(true, { fetchOnMount: false })
  const [copied, setCopied] = useState<boolean | null>(null)
  const direct = caps.capabilities.tailnet_direct
  if (message === undefined && !tailnetDirectActive(direct)) return null
  const copyMessage = async () => {
    const text = message ?? buildInviteMessage({ owner: ownerDisplayName(people.data?.people ?? []), urls: tailnetDirectActive(direct) ? direct.urls : [], inviteUrl })
    let ok = false
    try { ok = await copyToClipboard(text) } catch { ok = false }
    setCopied(ok)
    if (ok) { onCopied?.(); setTimeout(() => setCopied(null), 2000) }
  }
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <Btn type="button" primary={primary} onClick={() => { void copyMessage() }} data-testid={testId}>
        {copied ? <Check size={14} className="lucide-inline" aria-hidden /> : <Copy size={14} className="lucide-inline" aria-hidden />}
        {' '}{copied ? i18nT('sharedCrew.invite_copied') : i18nT(message === undefined ? 'sharedCrew.invite_copy_tailscale' : 'sharedCrew.invite_copy_ssm')}
      </Btn>
      {/* No hand-off: the invite draft and the People page around this button may hold unsaved input. */}
      {copied === false && <ErrorNotice variant="inline" message={i18nT('sharedCrew.direct_link_copy_failed')} testId={`${testId}-error`} />}
    </div>
  )
}

/** Allow sharing when no warning is due, or after the owner confirms it. The
 *  acknowledgment is per Crew, so one covers every sharing path; *transport*
 *  only words who can open it and where to stop sharing. */
export function useFirstShareGate(transport: InviteTransport = 'tailscale') {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [shareError, setShareError] = useState('')
  const confirmShare = useCallback(async () => {
    setShareError('')
    // A fresh read at the moment of sharing: the warning is due only while
    // nobody else has access. A gateway without the route reads as null; a
    // failed read stops here with its own notice, and the draft stays.
    let view: FirstShareView | null = null
    try {
      view = await qc.fetchQuery({ queryKey: sharedCrewAccessKey, queryFn: () => api.sharedCrewAccess(), staleTime: 0 })
    } catch (err) {
      setShareError(inviteErrorCode(err) || 'error')
      return false
    }
    const share = view?.first_share
    if (!share?.needs_ack) return true
    const ok = await confirm({
      title: i18nT('sharedCrew.first_share_title'),
      body: <FirstShareBody share={share} transport={transport} />,
      confirmLabel: i18nT('sharedCrew.first_share_confirm'),
      danger: false,
      primary: true,
    })
    if (!ok) return false
    try {
      await api.sharedCrewAckFirstShare()
    } catch (err) {
      // The server would refuse the invite unacknowledged, so stop with the
      // acknowledgment's own notice.
      setShareError(inviteErrorCode(err) || 'error')
      return false
    }
    await qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    return true
  }, [confirm, qc, transport])
  return { confirmShare, firstShareDialog: confirmDialog, shareError }
}

/** One friendly sentence per gateway refusal code. Route refusals carry the
 *  `tailscale_` prefix; the status read's `sync.error` carries the same
 *  upstream codes bare, plus the sync's own `backing_off`. */
export function inviteErrorText(code: string): string {
  switch (code) {
    case 'invalid_key_format': return i18nT('sharedCrew.invite_err_key_format')
    case 'tailscale_auth_failed':
    case 'auth_failed': return i18nT('sharedCrew.invite_err_auth')
    case 'tailscale_forbidden':
    case 'forbidden': return i18nT('sharedCrew.invite_err_forbidden')
    case 'device_not_found': return i18nT('sharedCrew.invite_err_device')
    case 'daemon_unavailable': return i18nT('sharedCrew.invite_err_daemon')
    case 'invalid_email': return i18nT('sharedCrew.invite_err_email')
    case 'tailscale_rate_limited':
    case 'rate_limited':
    case 'backing_off': return i18nT('sharedCrew.invite_err_rate')
    case 'avatar_too_large': return i18nT('sharedCrew.invite_err_avatar_size')
    case 'avatar_type': return i18nT('sharedCrew.invite_err_avatar_type')
    case 'owner_only': return i18nT('sharedCrew.invite_err_owner')
    default: return i18nT('sharedCrew.invite_err_generic')
  }
}

const acceptedCount = (status: TailscaleInvitesStatus | null | undefined) => (status?.invites ?? []).filter(i => i.status === 'accepted').length

export function useInvites() {
  const qc = useQueryClient()
  const caps = useSharedCrewCapabilities({ fetchOnMount: false })
  const pinned = invitesPinnedOff(caps.capabilities)
  const query = useQuery({
    queryKey: tailscaleInvitesKey,
    queryFn: async () => {
      const next = await api.tailscaleInvites()
      // An invite that turned accepted leaves the invite surfaces: the person's
      // row has to be there to take its place.
      const before = qc.getQueryData<TailscaleInvitesStatus | null>(tailscaleInvitesKey)
      if (acceptedCount(next) > acceptedCount(before)) void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
      return next
    },
    // An enterprise policy that pins tailnet access off closes the invite
    // routes (404), so nothing is read and no Invite… control appears.
    enabled: !caps.isPending && !pinned,
    retry: false,
    // While someone has not accepted yet, re-read so the row turns to accepted.
    refetchInterval: q => ((q.state.data as TailscaleInvitesStatus | null | undefined)?.invites ?? []).some(i => i.status === 'pending') ? 30_000 : false,
  })
  // A pin that lands after a read leaves cached data behind: hide it too, so
  // no invite or credential control is offered against routes that now 404.
  return pinned ? ({ ...query, data: undefined, error: null, isError: false, isSuccess: false } as unknown as typeof query) : query
}

/** One owner per visible surface for failed reads, including a cached sync failure. */
export function InviteReadError({ query, testId, askAgent = false }: {
  query: ReturnType<typeof useInvites>
  testId: string
  askAgent?: boolean
}) {
  const code = query.isError ? inviteErrorCode(query.error) || 'error' : query.data?.sync?.error
  if (!code || (!query.data && code === 'owner_only')) return null
  return (
    <div>
      {/* No hand-off: cached content or the surrounding People list may hold an unsaved name, picture, key or invite draft. */}
      <ErrorNotice message={inviteErrorText(code)} askAgent={askAgent} testId={query.isError ? testId : 'invite-sync-error'} />
      <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
    </div>
  )
}

function ExtLink({ href, label, testId }: { href: string; label: string; testId: string }) {
  return (
    <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      {label}
      <ExternalLink size={12} className="lucide-inline" aria-hidden />
    </a>
  )
}

function ErrorLine({ code, testId }: { code: string; testId: string }) {
  if (!code) return null
  // No hand-off: each line sits beside a form whose unsaved draft (key, emails,
  // picture) the hand-off would leave behind.
  return <ErrorNotice variant="inline" message={inviteErrorText(code)} testId={testId} />
}

/** A client-side check on a field, not a failure: plain text, not an `ErrorNotice`. */
function FieldHint({ code, id }: { code: string; id: string }) {
  if (!code) return null
  return <span id={id} role="status" className="text-[12px] text-text" data-testid={id}>{inviteErrorText(code)}</span>
}

/** No key yet: the manual share path, and the one-time key form. */
function ConnectForm() {
  const qc = useQueryClient()
  const [key, setKey] = useState('')
  const connect = useMutation({
    mutationFn: (k: string) => api.tailscaleConnect(k),
    onSuccess: data => { setKey(''); qc.setQueryData(tailscaleInvitesKey, data) },
  })
  const valid = looksLikeApiKey(key)
  return (
    <div className="flex flex-col gap-3 min-w-0" data-testid="invite-connect">
      <ExtLink href={TAILSCALE_ADMIN_MACHINES_URL} label={i18nT('sharedCrew.invite_manual_link')} testId="invite-admin-share" />
      <div className="flex flex-col gap-1.5 pt-2 border-t border-border min-w-0">
        <label htmlFor="tailscale-api-key" className="text-sm font-medium text-text-strong">{i18nT('sharedCrew.invite_key_label')}</label>
        <p className="text-[13px] text-muted" data-testid="invite-key-help">{i18nT('sharedCrew.invite_key_help')}</p>
        <ExtLink href={TAILSCALE_ADMIN_KEYS_URL} label={i18nT('sharedCrew.invite_key_link')} testId="invite-admin-keys" />
        <form
          className="flex flex-wrap gap-2 items-center min-w-0"
          onSubmit={e => { e.preventDefault(); if (valid && !connect.isPending) connect.mutate(key.trim()) }}
        >
          <Input
            id="tailscale-api-key"
            type="password"
            autoComplete="off"
            spellCheck={false}
            className="flex-1 min-w-[12rem]"
            value={key}
            disabled={connect.isPending}
            onChange={e => setKey(e.target.value)}
            data-testid="invite-key-input"
          />
          <Btn type="submit" primary disabled={!valid || connect.isPending} data-testid="invite-key-connect">
            {connect.isPending ? i18nT('sharedCrew.invite_key_connecting') : i18nT('sharedCrew.invite_key_connect')}
          </Btn>
        </form>
        <ErrorLine code={connect.isError ? inviteErrorCode(connect.error) || 'error' : ''} testId="invite-key-error" />
      </div>
    </div>
  )
}

/** After an email invite: Tailscale's email carries no Crew address, so the
 *  owner sends the message that does, or is told which setting gives one. */
function EmailInviteResult() {
  const direct = useSharedCrewCapabilities({ fetchOnMount: false }).capabilities.tailnet_direct
  const active = tailnetDirectActive(direct)
  const off = direct?.state === 'off'
  return (
    <div className="flex flex-wrap items-center gap-2 min-w-0" data-testid="invite-email-result">
      {direct && (
        <span className="text-[13px] text-muted w-full" data-testid="invite-email-hint">
          {active
            ? i18nT('sharedCrew.invite_email_hint')
            : off
              ? i18nT('sharedCrew.invite_email_hint_off', { setting: i18nT('sharedCrew.adv_direct') })
              : i18nT('sharedCrew.invite_email_hint_unavailable')}
        </span>
      )}
      <CopyInviteMessage testId="invite-copy-email-message" />
    </div>
  )
}

function InviteForm({ disabled }: { disabled: boolean }) {
  const qc = useQueryClient()
  const { confirmShare, firstShareDialog, shareError } = useFirstShareGate()
  const [mode, setMode] = useState<'email' | 'link'>('email')
  const [emails, setEmails] = useState('')
  const [alias, setAlias] = useState('')
  const [reusable, setReusable] = useState(false)
  const [avatarId, setAvatarId] = useState('')
  const [avatarErr, setAvatarErr] = useState('')
  const [avatarHint, setAvatarHint] = useState('')
  const [lastLink, setLastLink] = useState('')
  const [sent, setSent] = useState(false)
  const [copied, setCopied] = useState<boolean | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const parsed = useMemo(() => parseEmails(emails), [emails])
  const badEmail = parsed.find(e => !looksLikeEmail(e)) ?? ''
  const create = useMutation({
    mutationFn: () => api.tailscaleCreateInvites({
      emails: mode === 'email' ? parsed : [],
      multi_use: mode === 'link' && reusable,
      alias,
      avatar_id: avatarId,
    }),
    onSuccess: res => {
      const link = res.invites.find(i => i.kind === 'link')?.invite_url ?? ''
      setLastLink(link)
      setSent(res.invites.length > 0)
      setCopied(null)
      setEmails('')
      setAlias('')
      setAvatarId('')
      void qc.invalidateQueries({ queryKey: tailscaleInvitesKey })
      void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    },
  })
  const submission = useGuardedMutation(create.mutateAsync, confirmShare)
  const draftLocked = disabled || submission.isPending
  const upload = useMutation({
    mutationFn: async (file: File) => api.tailscaleUploadAvatar(await fileToBase64(file)),
    onSuccess: res => { setAvatarId(res.avatar_id); setAvatarErr('') },
    onError: err => setAvatarErr(inviteErrorCode(err) || 'error'),
  })
  const onPick = (file: File | undefined) => {
    if (!file || draftLocked || upload.isPending) return
    setAvatarErr('')
    if (!AVATAR_TYPES.includes(file.type)) { setAvatarHint('avatar_type'); return }
    if (file.size > AVATAR_MAX_BYTES) { setAvatarHint('avatar_too_large'); return }
    setAvatarHint('')
    upload.mutate(file)
  }
  const canSend = mode === 'link' || (parsed.length > 0 && !badEmail)
  return (
    <form
      className="flex flex-col gap-3 min-w-0"
      data-testid="invite-form"
      onSubmit={e => { e.preventDefault(); if (canSend && !disabled && !upload.isPending) void submission.run() }}
    >
      {firstShareDialog}
      <div role="tablist" aria-label={i18nT('sharedCrew.invite_dialog_title')} className="flex flex-wrap gap-2">
        <Btn type="button" role="tab" aria-selected={mode === 'email'} primary={mode === 'email'} disabled={draftLocked} onClick={() => setMode('email')} data-testid="invite-mode-email">
          <Mail size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.invite_by_email')}
        </Btn>
        <Btn type="button" role="tab" aria-selected={mode === 'link'} primary={mode === 'link'} disabled={draftLocked} onClick={() => setMode('link')} data-testid="invite-mode-link">
          <Link2 size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.invite_by_link')}
        </Btn>
      </div>
      {mode === 'email' ? (
        <div className="flex flex-col gap-1 min-w-0">
          <label htmlFor="invite-emails" className="text-[13px] text-text-strong">{i18nT('sharedCrew.invite_emails_label')}</label>
          <textarea
            id="invite-emails"
            aria-label={i18nT('sharedCrew.invite_emails_label')}
            rows={2}
            className="w-full min-w-0 rounded border border-border bg-bg text-text text-[13px] p-2"
            value={emails}
            disabled={draftLocked}
            onChange={e => setEmails(e.target.value)}
            aria-invalid={badEmail ? true : undefined}
            aria-describedby={badEmail ? 'invite-email-hint' : undefined}
            data-testid="invite-emails"
          />
          <FieldHint code={badEmail ? 'invalid_email' : ''} id="invite-email-hint" />
        </div>
      ) : (
        <div className="flex items-center gap-2 min-w-0" data-testid="invite-link-options">
          <Toggle checked={reusable} onChange={setReusable} disabled={draftLocked} label={i18nT('sharedCrew.invite_reusable')} />
          <span className="text-[13px] text-text">{i18nT('sharedCrew.invite_reusable')}</span>
        </div>
      )}
      <div className="flex flex-wrap gap-3 items-end min-w-0">
        <div className="flex flex-col gap-1 min-w-0 flex-1">
          <label htmlFor="invite-alias" className="text-[13px] text-text-strong">{i18nT('sharedCrew.invite_alias_label')}</label>
          <Input id="invite-alias" maxLength={64} value={alias} disabled={draftLocked} onChange={e => setAlias(e.target.value)} data-testid="invite-alias" />
        </div>
        <div className="flex items-center gap-2">
          {avatarId && <img src={avatarUrl(avatarId)} alt="" className="w-8 h-8 rounded-full object-cover" data-testid="invite-avatar-preview" />}
          <input ref={fileRef} type="file" disabled={draftLocked || upload.isPending} accept={AVATAR_TYPES.join(',')} className="sr-only" aria-label={i18nT('sharedCrew.invite_avatar')} onChange={e => { onPick(e.target.files?.[0]); e.target.value = '' }} data-testid="invite-avatar-input" />
          <Btn type="button" onClick={() => fileRef.current?.click()} disabled={draftLocked || upload.isPending} aria-describedby={avatarHint ? 'invite-avatar-hint' : undefined}>{i18nT('sharedCrew.invite_avatar')}</Btn>
        </div>
      </div>
      <FieldHint code={avatarHint} id="invite-avatar-hint" />
      <ErrorLine code={avatarErr} testId="invite-avatar-error" />
      <Btn type="submit" primary className="self-start" disabled={!canSend || draftLocked || upload.isPending} data-testid="invite-send">
        <UserPlus size={14} className="lucide-inline" aria-hidden /> {mode === 'email' ? i18nT('sharedCrew.invite_send') : i18nT('sharedCrew.invite_make_link')}
      </Btn>
      <ErrorLine code={shareError} testId="invite-share-error" />
      <ErrorLine code={create.isError ? inviteErrorCode(create.error) || 'error' : ''} testId="invite-send-error" />
      {lastLink && (
        <div className="flex flex-wrap items-center gap-2 min-w-0" data-testid="invite-link-result">
          <code className="text-[12px] break-all min-w-0 flex-1">{lastLink}</code>
          <Btn type="button" onClick={() => { void copyToClipboard(lastLink).then(ok => setCopied(ok)) }} data-testid="invite-copy-link">
            <Copy size={14} className="lucide-inline" aria-hidden /> {copied ? i18nT('sharedCrew.invite_copied') : i18nT('sharedCrew.invite_copy_link')}
          </Btn>
          {/* No hand-off: navigation leaves behind the unsaved invite draft (emails, name and picture). */}
          {copied === false && <ErrorNotice variant="inline" message={i18nT('sharedCrew.direct_link_copy_failed')} testId="invite-copy-error" />}
          <CopyInviteMessage inviteUrl={lastLink} testId="invite-copy-link-message" />
          <span className="text-[12px] text-muted w-full">{i18nT('sharedCrew.invite_link_warning')}</span>
        </div>
      )}
      {sent && !lastLink && <EmailInviteResult />}
    </form>
  )
}

/** Revoke with a confirm step: the one revoke control, on an invite row and on
 *  a joined person's row, whichever path the invite came by (*transport*); the button,
 *  the confirm and Cancel read the same for every path. *children* (a row's other actions) hide while the
 *  confirm shows. A link, or a row with no person's name, has no one to name:
 *  everyone who joined with it loses access. Otherwise the sentence follows the
 *  row's state: an unaccepted invite, an accepted share not connected yet, or a
 *  connected member. */
export type RevokeState = 'sent' | 'accepted' | 'member'

const REVOKE_BODY: Record<RevokeState, string> = {
  sent: 'sharedCrew.invite_revoke_body',
  accepted: 'sharedCrew.invite_revoke_accepted_body',
  member: 'sharedCrew.person_revoke_body',
}

export function RevokeControl({ inviteId, name, link = false, state = 'sent', children, transport = 'tailscale' }: { inviteId: string; name: string; link?: boolean; state?: RevokeState; children?: ReactNode; transport?: InviteTransport }) {
  const qc = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const revoke = useMutation({
    mutationFn: async (): Promise<void> => {
      if (transport === 'ssm') await api.sharedCrewSsmRevokeInvite(inviteId)
      else await api.tailscaleRevokeInvite(inviteId)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: transport === 'ssm' ? ssmStatusKey : tailscaleInvitesKey })
      void qc.invalidateQueries({ queryKey: sharedCrewKeys.people })
    },
  })
  return (
    <>
      <div className="flex flex-wrap gap-2">
        {!confirming && children}
        {confirming ? (
          <>
            <Btn type="button" danger onClick={() => revoke.mutate()} disabled={revoke.isPending} data-testid="invite-revoke-confirm">
              {i18nT('sharedCrew.invite_revoke_confirm')}
            </Btn>
            <Btn type="button" onClick={() => setConfirming(false)}>{i18nT('sharedCrew.invite_cancel')}</Btn>
          </>
        ) : (
          <Btn type="button" danger onClick={() => setConfirming(true)} data-testid="invite-revoke">
            <Trash2 size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.invite_revoke')}
          </Btn>
        )}
      </div>
      {confirming && <p className="text-[12px] text-muted w-full">{link || !name.trim() ? i18nT('sharedCrew.invite_revoke_link_body') : i18nT(REVOKE_BODY[state], { name })}</p>}
      <ErrorLine code={revoke.isError ? inviteErrorCode(revoke.error) || 'error' : ''} testId="invite-revoke-error" />
    </>
  )
}

/** An invite whose person has not joined, under the "Invites" heading, with
 *  Resend (a pending email invite) and Remove. Only the text fades, so the
 *  buttons read as live. An accepted share whose person has not connected yet
 *  reads Accepted. Used under "Invites" in the People list and for a link in
 *  the invite dialog. */
export function InviteRow({ invite }: { invite: InviteRecord }) {
  const resend = useMutation({ mutationFn: () => api.tailscaleResendInvite(invite.id) })
  const who = invite.accepted_login || invite.email || i18nT('sharedCrew.invite_link_row')
  const name = invite.alias || who
  const otherAccount = invite.accepted_login && invite.email && invite.email.trim().toLowerCase() !== invite.accepted_login.trim().toLowerCase()
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`invite-${invite.id}`} data-status={invite.status}>
      {invite.avatar_id ? (
        <img src={avatarUrl(invite.avatar_id)} alt="" className="w-8 h-8 rounded-full object-cover shrink-0" />
      ) : (
        <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
          {(name.trim()[0] ?? '?').toUpperCase()}
        </span>
      )}
      <div className="flex flex-col gap-0.5 min-w-0 flex-1 opacity-60">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {invite.kind === 'link' && invite.multi_use
            ? <Badge variant="muted">{i18nT('sharedCrew.invite_reusable')}</Badge>
            : invite.status === 'accepted'
              ? <Badge variant="ok">{i18nT('sharedCrew.invite_status_accepted')}</Badge>
              : invite.kind === 'email' && <Badge variant="muted">{i18nT('sharedCrew.invite_status_sent')}</Badge>}
        </div>
        {name !== who && <span className="text-[12px] text-muted truncate" title={who}>{who}</span>}
        {otherAccount && <span className="text-[12px] text-muted" data-testid="invite-other-account">{i18nT('sharedCrew.invite_other_account', { email: invite.email })}</span>}
        {invite.created_at > 0 && <span className="text-[12px] text-muted">{i18nT(invite.kind === 'link' ? 'sharedCrew.invite_created_at' : 'sharedCrew.invite_sent_at', { when: fmtDateTime(invite.created_at * 1000) })}</span>}
      </div>
      <RevokeControl inviteId={invite.id} name={name} link={invite.kind === 'link'} state={invite.status === 'accepted' ? 'accepted' : 'sent'}>
        {invite.kind === 'email' && invite.status === 'pending' && (
          <Btn type="button" onClick={() => resend.mutate()} disabled={resend.isPending || resend.isSuccess} data-testid="invite-resend">
            <RefreshCw size={14} className="lucide-inline" aria-hidden /> {resend.isSuccess ? i18nT('sharedCrew.invite_resent') : i18nT('sharedCrew.invite_resend')}
          </Btn>
        )}
      </RevokeControl>
      <ErrorLine code={resend.isError ? inviteErrorCode(resend.error) || 'error' : ''} testId="invite-row-error" />
    </li>
  )
}

function Connected({ status }: { status: TailscaleInvitesStatus }) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const disconnect = useMutation({
    mutationFn: () => api.tailscaleDisconnect(),
    onSuccess: data => qc.setQueryData(tailscaleInvitesKey, data),
  })
  // Removing the key cannot be undone, so it asks first.
  const removal = useGuardedMutation(disconnect.mutateAsync, () => confirm({
    title: i18nT('sharedCrew.invite_key_remove_title'),
    body: i18nT('sharedCrew.invite_key_remove_body'),
    confirmLabel: i18nT('sharedCrew.invite_key_remove'),
  }))
  return (
    <div className="flex flex-col gap-3 min-w-0" data-testid="invite-connected">
      <div className="flex flex-wrap items-center gap-2 min-w-0">
        <span className="text-[13px] text-text">{i18nT('sharedCrew.invite_connected_as', { tailnet: status.tailnet || '—' })}</span>
        {status.key_expired && <Badge variant="err">{i18nT('sharedCrew.invite_key_expired')}</Badge>}
        {!status.key_expired && status.key_expires_soon && <Badge variant="warn">{i18nT('sharedCrew.invite_key_expires_soon')}</Badge>}
        <Btn type="button" onClick={() => { void removal.run() }} disabled={removal.isPending} data-testid="invite-disconnect">{i18nT('sharedCrew.invite_key_remove')}</Btn>
        {confirmDialog}
      </div>
      <ErrorLine code={disconnect.isError ? inviteErrorCode(disconnect.error) || 'error' : ''} testId="invite-disconnect-error" />
      {status.key_expires && (
        <p className="text-[12px] text-muted" data-testid="invite-key-expiry">
          {i18nT('sharedCrew.invite_key_expires_at', { when: fmtDateTime(Date.parse(status.key_expires)) })}
        </p>
      )}
      {status.sync?.restart_required && (
        <p className="text-[12px] text-warn" data-testid="invite-restart">{i18nT('sharedCrew.invite_restart_needed')}</p>
      )}
      <InviteForm disabled={removal.isPending} />
    </div>
  )
}

/** The links an owner holds: reusable ones stay live after people join through
 *  them, and a link names nobody, so they live here and not in People. */
function HeldLinks({ invites }: { invites: readonly TailscaleInvite[] }) {
  const people = useSharedCrewPeople(true, { fetchOnMount: false })
  const links = heldLinks(people.data?.people ?? [], invites)
  if (links.length === 0) return null
  return (
    <ul className="flex flex-col divide-y divide-border pt-2 border-t border-border" data-testid="invite-links">
      {links.map(i => <InviteRow key={i.id} invite={i} />)}
    </ul>
  )
}

/** The Tailscale path of the invite dialog: the API key first when none is
 *  connected (with the by-hand share path beside it), then the invite form and
 *  the links held. Each sharing path adds its own pane to the dialog. */
function TailscalePane({ status }: { status: TailscaleInvitesStatus }) {
  return status.connected ? (
    <>
      <Connected status={status} />
      <HeldLinks invites={status.invites} />
    </>
  ) : (
    <>
      <p className="text-[13px] text-muted" data-testid="invite-key-first">{i18nT('sharedCrew.invite_dialog_key_first')}</p>
      <ConnectForm />
    </>
  )
}

/** The People page's "Invite…" dialog. Invites not yet accepted show in the
 *  People list as Invites rows. AWS SSM is a second pane, shown as a tab only
 *  when Tailscale invites are on too. */
export function InviteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const query = useInvites()
  const ssm = useSsmStatus()
  const status = query.data
  const ssmStatus = ssmConfigured(ssm.data) ? ssm.data : null
  const [picked, setPicked] = useState<InviteTransport | null>(null)
  // The pane an open dialog first showed stays, so a read that lands late never
  // swaps a half-filled form (or a link shown once) for the other path's.
  const first = useRef<InviteTransport | null>(null)
  if (!open) first.current = null
  else if (first.current === null && (status || ssmStatus)) first.current = ssmStatus && !status ? 'ssm' : 'tailscale'
  const both = !!status && !!ssmStatus
  const transport: InviteTransport = both ? picked ?? first.current ?? 'tailscale' : ssmStatus && !status ? 'ssm' : 'tailscale'
  return (
    <Modal open={open} onClose={onClose} title={i18nT('sharedCrew.invite_dialog_title')} maxWidth={520} guardAccidentalDismiss>
      <div className="flex flex-col gap-3 min-w-0" data-testid="invite-dialog">
        {both && (
          <div role="tablist" aria-label={i18nT('sharedCrew.invite_dialog_title')} className="flex flex-wrap gap-2">
            <Btn type="button" role="tab" aria-selected={transport === 'tailscale'} primary={transport === 'tailscale'} onClick={() => setPicked('tailscale')} data-testid="invite-transport-tailscale">
              {i18nT('sharedCrew.setup_tailscale')}
            </Btn>
            <Btn type="button" role="tab" aria-selected={transport === 'ssm'} primary={transport === 'ssm'} onClick={() => setPicked('ssm')} data-testid="invite-transport-ssm">
              {i18nT('sharedCrew.setup_ssm')}
            </Btn>
          </div>
        )}
        {/* Both panes stay mounted, so switching tabs never drops a link shown once or a draft. */}
        <SsmReadError query={ssm} testId="invite-dialog-ssm-error" />
        {ssmStatus && <div hidden={transport !== 'ssm'} className="flex flex-col gap-3 min-w-0"><SsmTab status={ssmStatus} /></div>}
        <div hidden={transport === 'ssm' && !!ssmStatus} className="flex flex-col gap-3 min-w-0">
          <InviteReadError query={query} testId="invite-dialog-error" />
          {status && <TailscalePane status={status} />}
        </div>
      </div>
    </Modal>
  )
}

/** The one place a person is invited from: the `Invite…` button and its dialog,
 *  with `children` (links that belong beside it) after the button. Renders the
 *  button only for the owner, whose invite read answered over either transport;
 *  a failed read shows its notice and retry here, once. */
export function InviteEntry({ children }: { children?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const query = useInvites()
  const ssmQuery = useSsmStatus()
  const canInvite = useCanInvite()
  return (
    <>
      {canInvite && (
        <div className="flex items-center gap-3 min-w-0 flex-wrap">
          <Btn onClick={() => setOpen(true)} data-testid="share-hero-invite">
            <UserPlus size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.hero_invite')}
          </Btn>
          {children}
        </div>
      )}
      {!open && <InviteReadError query={query} testId="people-invites-error" />}
      {!open && <SsmReadError query={ssmQuery} testId="people-ssm-error" />}
      {canInvite && <InviteDialog open={open} onClose={() => setOpen(false)} />}
    </>
  )
}

/** Whether the viewer is the owner: an invite read answered, over Tailscale or
 *  over AWS SSM. */
export function useCanInvite(): boolean {
  const tailscale = useInvites().data
  const ssm = useSsmStatus().data
  return !!tailscale || ssmConfigured(ssm)
}

/** Whether an owner-only invite read answered at all, even an SSM read that says the
 *  path is off: the owner reaches Advanced (where SSM is switched on) without an
 *  invite path being up. Invite controls ask `useCanInvite` instead. */
export function useOwnerRead(): boolean {
  const tailscale = useInvites().data
  const ssm = useSsmStatus().data
  return !!tailscale || !!ssm
}
