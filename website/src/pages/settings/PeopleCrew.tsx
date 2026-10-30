/** Settings > People on a Crew shared over Tailscale (layout C).
 *
 *  Top to bottom: a share card (the live listener state, who is here now, the
 *  share link with Copy link and Invite..., and the Tailscale users page), then
 *  the People list, then Remote Control. Explanations sit in the ⓘ beside each
 *  heading.
 *
 *  Any admitted person can edit any person's display name and picture (the ✎ on
 *  each row with a known Tailscale user id). It is display only: the verified
 *  login stays on the row, and who can connect is decided in Tailscale. */
import { useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, Pencil, UserPlus } from 'lucide-react'
import { api } from '../../api/client'
import type { SetupPathCapability, TailnetDirect } from '../../api/sharedCrew'
import { AVATAR_MAX_BYTES, AVATAR_TYPES, avatarUrl, fileToBase64, inviteErrorCode } from '../../api/sharedCrewInvites'
import { principalInitials, principalName, type PeopleRow } from '../../api/sharedCrewUi'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { tailnetDirectActive } from '../../components/TailnetDirectLink'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { TAILSCALE_USERS_URL, headingTip, statusBadge } from './PeopleModes'
import { InviteDialog, InviteRow, inviteErrorText, useInvites } from './TailscaleInvites'

/** At most this many faces in the "here now" stack; the count says the rest. */
const STACK_MAX = 4

function displayName(row: PeopleRow): string {
  return row.member?.alias || principalName(row.principal) || i18nT('sharedCrew.unattributed')
}

function Avatar({ row, size = 32 }: { row: PeopleRow; size?: number }) {
  const style = { width: size, height: size }
  return row.member?.avatar_id ? (
    <img src={avatarUrl(row.member.avatar_id)} alt="" style={style} className="rounded-full object-cover shrink-0" data-testid="person-avatar" />
  ) : (
    <span style={style} className="inline-flex items-center justify-center rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
      {principalInitials(row.principal)}
    </span>
  )
}

function HereDot({ label }: { label: string }) {
  return <span className="inline-block w-2 h-2 rounded-full bg-ok shrink-0" role="img" aria-label={label} />
}

/** The share card. `canInvite` is true only for the owner (the invite read answered). */
export function ShareHero({ direct, tailscale, people, canInvite }: {
  direct: TailnetDirect | undefined
  tailscale: SetupPathCapability | undefined
  people: PeopleRow[]
  canInvite: boolean
}) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const [inviting, setInviting] = useState(false)
  const active = tailnetDirectActive(direct)
  const here = people.filter(row => row.live)
  const [primary, ...fallbacks] = active ? direct.urls : []
  const copy = async () => {
    let ok = false
    try { ok = await copyToClipboard(primary) } catch { ok = false }
    setCopied(ok)
    if (ok) setTimeout(() => setCopied(null), 2000)
  }
  return (
    <SettingsCard>
      <div className="flex flex-col gap-3 min-w-0" data-testid="share-hero">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          {active ? (
            <span className="inline-flex items-center gap-2 text-sm font-semibold text-text-strong" data-testid="share-hero-state">
              <HereDot label={i18nT('sharedCrew.hero_shared')} />
              {i18nT('sharedCrew.hero_shared')}
            </span>
          ) : (
            <span className="inline-flex items-center gap-2 text-sm font-semibold text-text-strong" data-testid="share-hero-state">
              {i18nT('sharedCrew.setup_tailscale')} {statusBadge(tailscale)}
            </span>
          )}
          {headingTip(i18nT('sharedCrew.hero_tip'), 'share-hero-tip')}
          {here.length > 0 && (
            <span className="ml-auto inline-flex items-center gap-2 min-w-0" data-testid="share-hero-here">
              <span className="inline-flex -space-x-2">
                {here.slice(0, STACK_MAX).map(row => (
                  <span key={row.principal.id} className="rounded-full ring-2 ring-[var(--bg)]" title={displayName(row)}><Avatar row={row} size={24} /></span>
                ))}
              </span>
              <span className="text-[13px] text-muted">{i18nT('sharedCrew.here_now', { n: fmtNumber(here.length) })}</span>
            </span>
          )}
        </div>
        {(active || canInvite) && (
          <div className="flex items-center gap-2 min-w-0 flex-wrap">
            {active && (
              <>
                <Input
                  readOnly
                  value={primary}
                  onFocus={e => e.currentTarget.select()}
                  aria-label={i18nT('sharedCrew.direct_link_label')}
                  className="flex-1 min-w-[12rem] font-mono text-[12px]"
                  data-testid="share-hero-link"
                />
                <Btn primary onClick={() => { void copy() }} data-testid="share-hero-copy">
                  {copied ? <Check size={14} className="lucide-inline" aria-hidden /> : <Copy size={14} className="lucide-inline" aria-hidden />}
                  {' '}{copied ? i18nT('sharedCrew.invite_copied') : i18nT('sharedCrew.hero_copy_link')}
                </Btn>
              </>
            )}
            {canInvite && (
              <Btn onClick={() => setInviting(true)} data-testid="share-hero-invite">
                <UserPlus size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.hero_invite')}
              </Btn>
            )}
          </div>
        )}
        {/* No hand-off: an invite draft may be open in the dialog. */}
        {copied === false && <ErrorNotice variant="inline" message={i18nT('sharedCrew.direct_link_copy_failed')} testId="share-hero-copy-error" />}
        {fallbacks.length > 0 && (
          <details className="text-[12px] text-muted">
            <summary className="cursor-pointer">{i18nT('sharedCrew.direct_link_fallbacks')}</summary>
            <ul className="flex flex-col gap-1 pt-1 font-mono break-all">
              {fallbacks.map(url => <li key={url}>{url}</li>)}
            </ul>
          </details>
        )}
        <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="share-hero-manage">
          {i18nT('sharedCrew.adapter_tailscale_manage')}
          <ExternalLink size={12} className="lucide-inline" aria-hidden />
        </a>
      </div>
      {canInvite && <InviteDialog open={inviting} onClose={() => setInviting(false)} />}
    </SettingsCard>
  )
}

function activityText(row: PeopleRow, usageAvailable: boolean): string {
  if (row.live?.activity === 'typing') {
    return row.live.session_title
      ? i18nT('sharedCrew.activity_typing_in', { title: row.live.session_title })
      : i18nT('sharedCrew.activity_typing')
  }
  if (usageAvailable && (row.usage?.active ?? 0) > 0) {
    return i18nT('sharedCrew.activity_running', { n: fmtNumber(row.usage?.active ?? 0) })
  }
  if (row.live) return i18nT('sharedCrew.here_now_label')
  return row.last_seen_at ? i18nT('sharedCrew.last_seen', { when: fmtDateTime(row.last_seen_at * 1000) }) : i18nT('sharedCrew.not_seen_live')
}

function PersonLine({ row, usageAvailable, onEdit }: { row: PeopleRow; usageAvailable: boolean; onEdit: (row: PeopleRow) => void }) {
  const name = displayName(row)
  const login = row.principal.display.login
  return (
    <li className="flex items-center gap-3 py-2 min-w-0" data-testid={`person-${row.principal.id}`}>
      <Avatar row={row} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {row.is_viewer && <Badge variant="muted">{i18nT('sharedCrew.you')}</Badge>}
          {row.principal.kind === 'machine' && <Badge variant="muted">{i18nT('sharedCrew.kind_machine')}</Badge>}
        </div>
        <span className="text-[12px] text-muted truncate" data-testid="person-activity">
          {login && login !== name ? <>{login}<span aria-hidden>{'\u00A0·\u00A0'}</span></> : null}
          {activityText(row, usageAvailable)}
        </span>
      </div>
      {row.live && <HereDot label={i18nT('sharedCrew.here_now_label')} />}
      {row.tailscale_user_id && (
        <Btn onClick={() => onEdit(row)} aria-label={i18nT('sharedCrew.edit_person', { name })} data-testid="person-edit">
          <Pencil size={14} aria-hidden />
        </Btn>
      )}
    </li>
  )
}

function EditPersonDialog({ row, onClose }: { row: PeopleRow; onClose: () => void }) {
  const qc = useQueryClient()
  const [alias, setAlias] = useState(row.member?.alias ?? '')
  const [picture, setPicture] = useState<{ base64: string; preview: string } | null>(null)
  const [clear, setClear] = useState(false)
  const [hint, setHint] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const save = useMutation({
    mutationFn: () => api.sharedCrewSetPersonDisplay(row.tailscale_user_id ?? '', {
      alias,
      ...(picture ? { avatar_data_base64: picture.base64 } : {}),
      ...(clear && !picture ? { clear_avatar: true } : {}),
    }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: sharedCrewKeys.people }); onClose() },
  })
  const onPick = async (file: File | undefined) => {
    if (!file) return
    if (!AVATAR_TYPES.includes(file.type)) { setHint('avatar_type'); return }
    if (file.size > AVATAR_MAX_BYTES) { setHint('avatar_too_large'); return }
    setHint('')
    setClear(false)
    setPicture({ base64: await fileToBase64(file), preview: URL.createObjectURL(file) })
  }
  const shown = picture?.preview || (!clear && row.member?.avatar_id ? avatarUrl(row.member.avatar_id) : '')
  const login = row.principal.display.login
  return (
    <Modal
      open
      onClose={() => { if (!save.isPending) onClose() }}
      title={i18nT('sharedCrew.edit_title')}
      maxWidth={440}
      guardAccidentalDismiss
      footer={(
        <div className="flex justify-end gap-2">
          <Btn onClick={onClose} disabled={save.isPending}>{i18nT('sharedCrew.invite_cancel')}</Btn>
          <Btn primary onClick={() => save.mutate()} disabled={save.isPending} data-testid="person-edit-save">
            {save.isPending ? i18nT('sharedCrew.edit_saving') : i18nT('sharedCrew.edit_save')}
          </Btn>
        </div>
      )}
    >
      <form className="flex flex-col gap-3 min-w-0" data-testid="person-edit-dialog" onSubmit={e => { e.preventDefault(); save.mutate() }}>
        <div className="flex items-center gap-3 min-w-0">
          {shown ? (
            <img src={shown} alt="" className="w-12 h-12 rounded-full object-cover shrink-0" data-testid="person-edit-preview" />
          ) : (
            <span className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-bg-hover text-text text-sm font-semibold shrink-0" aria-hidden>
              {principalInitials(row.principal)}
            </span>
          )}
          <div className="flex flex-col gap-1 min-w-0">
            {login && <span className="text-[12px] text-muted truncate" data-testid="person-edit-login">{login}</span>}
            <div className="flex gap-2 flex-wrap">
              <input ref={fileRef} type="file" accept={AVATAR_TYPES.join(',')} className="sr-only" aria-label={i18nT('sharedCrew.edit_picture')} onChange={e => { void onPick(e.target.files?.[0]); e.target.value = '' }} data-testid="person-edit-picture-input" />
              <Btn type="button" onClick={() => fileRef.current?.click()} disabled={save.isPending}>{i18nT('sharedCrew.edit_picture')}</Btn>
              {shown && (
                <Btn type="button" onClick={() => { setPicture(null); setClear(true) }} disabled={save.isPending} data-testid="person-edit-remove-picture">
                  {i18nT('sharedCrew.edit_remove_picture')}
                </Btn>
              )}
            </div>
          </div>
        </div>
        {hint && <span className="text-[12px] text-text" data-testid="person-edit-hint">{inviteErrorText(hint)}</span>}
        <label className="flex flex-col gap-1 min-w-0">
          <span className="text-[13px] text-text-strong">{i18nT('sharedCrew.edit_name_label')}</span>
          <Input maxLength={64} value={alias} placeholder={principalName(row.principal)} onChange={e => setAlias(e.target.value)} disabled={save.isPending} data-testid="person-edit-name" />
        </label>
        <p className="text-[12px] text-muted">{i18nT('sharedCrew.edit_note')}</p>
        {/* No hand-off: the dialog holds the unsaved name and picture. */}
        {save.isError && <ErrorNotice variant="inline" message={inviteErrorText(inviteErrorCode(save.error) || 'error')} testId="person-edit-error" />}
      </form>
    </Modal>
  )
}

/** "People · N": everyone connected or seen, then invites nobody has accepted yet. */
export function PeopleList({ people, usageAvailable, tipExtra, footer }: {
  people: PeopleRow[]
  usageAvailable: boolean
  tipExtra: string
  footer?: ReactNode
}) {
  const [editing, setEditing] = useState<PeopleRow | null>(null)
  const invites = useInvites()
  const pending = (invites.data?.invites ?? []).filter(i => i.status === 'pending')
  const tip = [i18nT('sharedCrew.people_tip'), tipExtra].filter(Boolean).join(' ')
  return (
    <SettingsSection title={i18nT('sharedCrew.people_title', { n: fmtNumber(people.length) })} badge={headingTip(tip, 'people-tip')}>
      <SettingsCard>
        {people.length === 0 && pending.length === 0 ? (
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.people_seen_empty')}</p>
        ) : (
          <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.people_title', { n: fmtNumber(people.length) })} data-testid="people-list">
            {people.map(row => <PersonLine key={row.principal.id} row={row} usageAvailable={usageAvailable} onEdit={setEditing} />)}
            {pending.map(invite => <InviteRow key={invite.id} invite={invite} inPeople />)}
          </ul>
        )}
        {footer}
      </SettingsCard>
      {editing && <EditPersonDialog row={editing} onClose={() => setEditing(null)} />}
    </SettingsSection>
  )
}

/** Whether the invite routes answered for this viewer (the owner). */
export function useCanInvite(): boolean {
  return !!useInvites().data
}
