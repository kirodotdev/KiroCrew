/** Settings > People > Invite over Tailscale.
 *
 *  One step for the owner: type teammates' emails (or make a link), optionally
 *  give them a name and a picture, and send. Tailscale mails the share invite;
 *  when the teammate accepts, Crew learns their real Tailscale login and lets
 *  that login in. The name and picture are display only: who gets in is always
 *  the login Tailscale reports, even when they accepted from another account.
 *
 *  Without a connected Tailscale API key the card explains the manual path and
 *  links the admin console's Machines page, where the owner shares this machine
 *  by hand. The key is sent once and never shown again. */
import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, ExternalLink, Link2, Mail, RefreshCw, Trash2, UserPlus } from 'lucide-react'
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
  type TailscaleInvite,
  type TailscaleInvitesStatus,
} from '../../api/sharedCrewInvites'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn, Input, Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { copyToClipboard } from '../../utils/clipboard'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { useGuardedMutation } from '../../hooks/sharedCrew/useGuardedMutation'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useConfirm } from '../../components/ConfirmDialog'
import { sharedCrewAccessKey, useFirstShareGate } from './TailscaleAccess'

export const tailscaleInvitesKey = [...sharedCrewKeys.all, 'tailscale-invites'] as const

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

function useInvites() {
  return useQuery({
    queryKey: tailscaleInvitesKey,
    queryFn: () => api.tailscaleInvites(),
    retry: false,
    // While someone has not accepted yet, re-read so the row turns to accepted.
    refetchInterval: q => ((q.state.data as TailscaleInvitesStatus | null | undefined)?.invites ?? []).some(i => i.status === 'pending') ? 30_000 : false,
  })
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
  return <span id={id} className="text-[12px] text-text" data-testid={id}>{inviteErrorText(code)}</span>
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
      <p className="text-[13px] text-muted">{i18nT('sharedCrew.invite_manual_body')}</p>
      <ExtLink href={TAILSCALE_ADMIN_MACHINES_URL} label={i18nT('sharedCrew.invite_manual_link')} testId="invite-admin-share" />
      <div className="flex flex-col gap-1.5 pt-2 border-t border-border min-w-0">
        <label htmlFor="tailscale-api-key" className="text-sm font-medium text-text-strong">{i18nT('sharedCrew.invite_key_label')}</label>
        <p className="text-[12px] text-muted">{i18nT('sharedCrew.invite_key_help')}</p>
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

function InviteForm({ disabled }: { disabled: boolean }) {
  const qc = useQueryClient()
  const { confirmShare, firstShareDialog } = useFirstShareGate()
  const [mode, setMode] = useState<'email' | 'link'>('email')
  const [emails, setEmails] = useState('')
  const [alias, setAlias] = useState('')
  const [reusable, setReusable] = useState(false)
  const [avatarId, setAvatarId] = useState('')
  const [avatarErr, setAvatarErr] = useState('')
  const [avatarHint, setAvatarHint] = useState('')
  const [lastLink, setLastLink] = useState('')
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
      <div role="tablist" aria-label={i18nT('sharedCrew.invite_title')} className="flex flex-wrap gap-2">
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
          <span className="text-[12px] text-muted">{i18nT('sharedCrew.invite_reusable_help')}</span>
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
          <Btn type="button" onClick={() => fileRef.current?.click()} disabled={draftLocked || upload.isPending}>{i18nT('sharedCrew.invite_avatar')}</Btn>
        </div>
      </div>
      <p className="text-[12px] text-muted">{i18nT('sharedCrew.invite_alias_help')}</p>
      <FieldHint code={avatarHint} id="invite-avatar-hint" />
      <ErrorLine code={avatarErr} testId="invite-avatar-error" />
      <Btn type="submit" primary className="self-start" disabled={!canSend || draftLocked || upload.isPending} data-testid="invite-send">
        <UserPlus size={14} className="lucide-inline" aria-hidden /> {mode === 'email' ? i18nT('sharedCrew.invite_send') : i18nT('sharedCrew.invite_make_link')}
      </Btn>
      <ErrorLine code={create.isError ? inviteErrorCode(create.error) || 'error' : ''} testId="invite-send-error" />
      {lastLink && (
        <div className="flex flex-wrap items-center gap-2 min-w-0" data-testid="invite-link-result">
          <code className="text-[12px] break-all min-w-0 flex-1">{lastLink}</code>
          <Btn type="button" onClick={() => { void copyToClipboard(lastLink).then(ok => setCopied(ok)) }} data-testid="invite-copy-link">
            <Copy size={14} className="lucide-inline" aria-hidden /> {copied ? i18nT('sharedCrew.invite_copied') : i18nT('sharedCrew.invite_copy_link')}
          </Btn>
          {/* No hand-off: navigation leaves behind the unsaved invite draft (emails, name and picture). */}
          {copied === false && <ErrorNotice variant="inline" message={i18nT('sharedCrew.direct_link_copy_failed')} testId="invite-copy-error" />}
          <span className="text-[12px] text-muted w-full">{i18nT('sharedCrew.invite_link_warning')}</span>
        </div>
      )}
    </form>
  )
}

function InviteRow({ invite }: { invite: TailscaleInvite }) {
  const qc = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const refresh = () => void qc.invalidateQueries({ queryKey: tailscaleInvitesKey })
  const resend = useMutation({ mutationFn: () => api.tailscaleResendInvite(invite.id) })
  const revoke = useMutation({
    mutationFn: () => api.tailscaleRevokeInvite(invite.id),
    onSuccess: () => { refresh(); void qc.invalidateQueries({ queryKey: sharedCrewKeys.people }) },
  })
  const who = invite.accepted_login || invite.email || i18nT('sharedCrew.invite_link_row')
  const name = invite.alias || who
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`invite-${invite.id}`} data-status={invite.status}>
      {invite.avatar_id ? (
        <img src={avatarUrl(invite.avatar_id)} alt="" className="w-8 h-8 rounded-full object-cover shrink-0" />
      ) : (
        <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
          {(name.trim()[0] ?? '?').toUpperCase()}
        </span>
      )}
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {invite.status === 'accepted'
            ? <Badge variant="ok">{i18nT('sharedCrew.invite_status_accepted')}</Badge>
            : <Badge variant="warn">{i18nT('sharedCrew.invite_status_pending')}</Badge>}
          {invite.kind === 'link' && <Badge variant="muted">{invite.multi_use ? i18nT('sharedCrew.invite_reusable') : i18nT('sharedCrew.invite_single_use')}</Badge>}
        </div>
        {name !== who && <span className="text-[12px] text-muted truncate" title={who}>{who}</span>}
        {invite.status === 'accepted' && invite.email && invite.accepted_login && invite.email.toLowerCase() !== invite.accepted_login.toLowerCase() && (
          <span className="text-[12px] text-muted" data-testid="invite-other-account">{i18nT('sharedCrew.invite_other_account', { email: invite.email })}</span>
        )}
        {invite.created_at > 0 && <span className="text-[12px] text-muted">{i18nT('sharedCrew.invite_sent_at', { when: fmtDateTime(invite.created_at * 1000) })}</span>}
      </div>
      <div className="flex flex-wrap gap-2">
        {!confirming && invite.kind === 'email' && invite.status === 'pending' && (
          <Btn type="button" onClick={() => resend.mutate()} disabled={resend.isPending || resend.isSuccess} data-testid="invite-resend">
            <RefreshCw size={14} className="lucide-inline" aria-hidden /> {resend.isSuccess ? i18nT('sharedCrew.invite_resent') : i18nT('sharedCrew.invite_resend')}
          </Btn>
        )}
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
      {confirming && <p className="text-[12px] text-muted w-full">{i18nT('sharedCrew.invite_revoke_body')}</p>}
      <ErrorLine code={revoke.isError ? inviteErrorCode(revoke.error) || 'error' : resend.isError ? inviteErrorCode(resend.error) || 'error' : ''} testId="invite-row-error" />
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
      <ErrorLine code={status.sync?.error ?? ''} testId="invite-sync-error" />
      {status.sync?.restart_required && (
        <p className="text-[12px] text-warn" data-testid="invite-restart">{i18nT('sharedCrew.invite_restart_needed')}</p>
      )}
      <InviteForm disabled={removal.isPending} />
      <div className="pt-2 border-t border-border">
        {status.invites.length === 0 ? (
          <p className="text-[13px] text-muted" data-testid="invite-empty">{i18nT('sharedCrew.invite_empty')}</p>
        ) : (
          <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.invite_list_title')}>
            {status.invites.map(i => <InviteRow key={i.id} invite={i} />)}
          </ul>
        )}
      </div>
    </div>
  )
}

/** The whole card. Hidden when this gateway has no invite routes (404). */
export function TailscaleInvites() {
  const query = useInvites()
  if (query.isLoading) return null
  const status = query.data
  const code = query.isError ? inviteErrorCode(query.error) : ''
  // Missing routes and an initial owner-only refusal intentionally hide the card.
  if (!status && (!query.isError || code === 'owner_only')) return null
  return (
    <SettingsSection title={i18nT('sharedCrew.invite_title')}>
      {query.isError && (
        <div>
          {/* No hand-off: cached content holds the unsaved key or invite emails, name and picture. */}
          <ErrorNotice message={inviteErrorText(code || 'error')} askAgent={!status} testId="invite-load-error" />
          <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      )}
      {status && <SettingsCard>
        <div className="flex flex-col gap-2 min-w-0" data-testid="tailscale-invites">
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.invite_body')}</p>
          {status.connected ? <Connected status={status} /> : <ConnectForm />}
        </div>
      </SettingsCard>}
    </SettingsSection>
  )
}
