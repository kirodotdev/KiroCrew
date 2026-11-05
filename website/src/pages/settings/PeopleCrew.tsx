/** Settings > People on a Crew shared over Tailscale.
 *
 *  Top to bottom: a connect card (the one Invite… entry, then the Crew link to
 *  send), then the People list. Labels and states only.
 *
 *  Any admitted person can edit any person's display name and picture (the ✎ on
 *  each row with a known Tailscale user id). It is display only: the verified
 *  login stays on the row, and who can connect is decided in Tailscale. */
import { useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, Pencil } from 'lucide-react'
import { api } from '../../api/client'
import type { SetupPathCapability, TailnetDirect } from '../../api/sharedCrew'
import { AVATAR_MAX_BYTES, AVATAR_TYPES, avatarPreviewUri, avatarUrl, fileToBase64, inviteErrorCode } from '../../api/sharedCrewInvites'
import { principalInitials, principalName, waitingInvites, type PeopleRow } from '../../api/sharedCrewUi'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn, Input, PanelSectionHeader } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { tailnetDirectActive } from '../../components/TailnetDirectLink'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { TAILSCALE_USERS_URL, statusBadge } from './PeopleModes'
import type { TailscaleInvite } from '../../api/sharedCrewInvites'
import { CopyInviteMessage, InviteEntry, InviteRow, RevokeControl, inviteErrorText, useInvites } from './TailscaleInvites'

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

/** Here now: an open tab in presence, or the viewer, who is on this page. */
function isHere(row: PeopleRow): boolean {
  return !!row.live || !!row.is_viewer
}

function HereDot({ label }: { label: string }) {
  return <span className="inline-block w-2 h-2 rounded-full bg-ok shrink-0" role="img" aria-label={label} />
}

/** The connect card. `canInvite` is true only for the owner (the invite read
 *  answered): the owner gets the Invite… entry and the invite message, a
 *  teammate only the Crew link. */
export function ShareHero({ direct, tailscale, people, canInvite }: {
  direct: TailnetDirect | undefined
  tailscale: SetupPathCapability | undefined
  people: PeopleRow[]
  canInvite: boolean
}) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const active = tailnetDirectActive(direct)
  const here = people.filter(isHere)
  const [primary, ...fallbacks] = active ? direct.urls : []
  const copy = async () => {
    let ok = false
    try { ok = await copyToClipboard(primary) } catch { ok = false }
    setCopied(ok)
    if (ok) setTimeout(() => setCopied(null), 2000)
  }
  return (
    // One bordered card: the settings cards are frameless, and this one is the
    // page's summary, so it carries its own surface.
    <div className="rounded-lg border border-border bg-card p-4 animate-rise">
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
          {here.length > 0 && (
            <span className="ml-auto inline-flex items-center gap-2 min-w-0" data-testid="share-hero-here">
              <span className="inline-flex gap-1">
                {here.slice(0, STACK_MAX).map(row => (
                  <span key={row.principal.id} className="rounded-full" title={displayName(row)}><Avatar row={row} size={24} /></span>
                ))}
              </span>
              <span className="text-[13px] text-muted">{i18nT('sharedCrew.here_now', { n: fmtNumber(here.length) })}</span>
            </span>
          )}
        </div>
        <InviteEntry>
          <a className="inline-flex items-center gap-1 text-[13px] text-accent" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="share-hero-manage">
            {i18nT('sharedCrew.adapter_tailscale_manage')}
            <ExternalLink size={12} className="lucide-inline" aria-hidden />
          </a>
        </InviteEntry>
        {active && (
          <div className="flex flex-col gap-2 min-w-0" data-testid="share-hero-link-row">
            <div className="flex items-center gap-2 min-w-0 flex-wrap">
              <Input
                readOnly
                value={primary}
                onFocus={e => e.currentTarget.select()}
                aria-label={i18nT('sharedCrew.direct_link_label')}
                className="flex-1 min-w-[12rem] font-mono text-[12px]"
                data-testid="share-hero-link"
              />
              {canInvite ? (
                <CopyInviteMessage primary testId="share-hero-copy-message" />
              ) : (
                <Btn primary onClick={() => { void copy() }} data-testid="share-hero-copy">
                  {copied ? <Check size={14} className="lucide-inline" aria-hidden /> : <Copy size={14} className="lucide-inline" aria-hidden />}
                  {' '}{copied ? i18nT('sharedCrew.invite_copied') : i18nT('sharedCrew.hero_copy_link')}
                </Btn>
              )}
            </div>
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
          </div>
        )}
      </div>
    </div>
  )
}

/** The row's state: what they are doing now, when they were last here, or
 *  "Sent" for a person Tailscale shares this machine with who has not
 *  connected. Empty for anyone else never seen. */
function activityText(row: PeopleRow, usageAvailable: boolean): string {
  if (row.live?.activity === 'typing') {
    return row.live.session_title
      ? i18nT('sharedCrew.activity_typing_in', { title: row.live.session_title })
      : i18nT('sharedCrew.activity_typing')
  }
  if (usageAvailable && (row.usage?.active ?? 0) > 0) {
    return i18nT('sharedCrew.activity_running', { n: fmtNumber(row.usage?.active ?? 0) })
  }
  if (isHere(row)) return i18nT('sharedCrew.here_now_label')
  if (row.last_seen_at) return i18nT('sharedCrew.last_seen', { when: fmtDateTime(row.last_seen_at * 1000) })
  return row.sources.includes('tailscale_share') ? i18nT('sharedCrew.invite_status_sent') : ''
}

/** The owner's way to remove someone: Remove on a person who came in by a
 *  single-use invite, else a link to the Tailscale users page, labelled as the
 *  link it is. A reusable link admitted many people, so revoking it would remove
 *  them all. */
function RemovePerson({ invite, name, member, children }: { invite: TailscaleInvite | undefined; name: string; member: boolean; children?: ReactNode }) {
  // The row's other button hides while the revoke confirm shows, so a row never holds three.
  if (invite && !invite.multi_use) return <RevokeControl inviteId={invite.id} name={name} state={member ? 'member' : 'sent'}>{children}</RevokeControl>
  return (
    <>
      <a className="inline-flex items-center gap-1 text-[13px] text-accent" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="person-remove">
        {i18nT('sharedCrew.person_remove_in_tailscale')}
        <ExternalLink size={12} className="lucide-inline" aria-hidden />
      </a>
      {children}
    </>
  )
}

/** One person. *invite* is the invite they came in by, known only to the owner,
 *  who is also the only one offered a way to remove them (*canRemove*). */
function PersonLine({ row, usageAvailable, invite, canRemove, onEdit }: {
  row: PeopleRow
  usageAvailable: boolean
  invite: TailscaleInvite | undefined
  canRemove: boolean
  onEdit: (row: PeopleRow) => void
}) {
  const name = displayName(row)
  const login = row.principal.display.login
  const activity = activityText(row, usageAvailable)
  const showLogin = !!login && login !== name
  // Tailscale lets an invite be accepted from any account: say so when the
  // person who got in is not the address the owner invited.
  // Only someone Tailscale shares this machine with can be removed there: a member
  // or a person with an invite. A session creator or a machine has nothing to remove.
  const removable = canRemove && !row.is_owner && !row.is_viewer && !!(row.member?.invite_id || invite)
  const edit = row.tailscale_user_id ? (
    <Btn onClick={() => onEdit(row)} aria-label={i18nT('sharedCrew.edit_person', { name })} data-testid="person-edit">
      <Pencil size={14} aria-hidden />
    </Btn>
  ) : null
  const otherAccount = invite?.email && login && invite.email.trim().toLowerCase() !== login.trim().toLowerCase() ? invite.email : ''
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`person-${row.principal.id}`}>
      <Avatar row={row} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {row.is_viewer && row.is_owner
            ? <Badge variant="muted">{i18nT('sharedCrew.you_owner')}</Badge>
            : row.is_viewer
              ? <Badge variant="muted">{i18nT('sharedCrew.you')}</Badge>
              : row.is_owner && <Badge variant="muted">{i18nT('sharedCrew.crew_owner')}</Badge>}
          {row.principal.kind === 'machine' && <Badge variant="muted">{i18nT('sharedCrew.kind_machine')}</Badge>}
        </div>
        {(showLogin || activity) && (
          <span className="text-[12px] text-muted truncate" data-testid="person-activity">
            {showLogin ? login : null}
            {showLogin && activity ? <span aria-hidden>{'\u00A0·\u00A0'}</span> : null}
            {activity}
          </span>
        )}
        {otherAccount && <span className="text-[12px] text-muted" data-testid="person-other-account">{i18nT('sharedCrew.invite_other_account', { email: otherAccount })}</span>}
      </div>
      {isHere(row) && <HereDot label={i18nT('sharedCrew.here_now_label')} />}
      {removable ? <RemovePerson invite={invite} name={name} member={!!row.member?.invite_id}>{edit}</RemovePerson> : edit}
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
  // A chosen picture is read before it can be sent; saving or changing the
  // picture waits for that read, so a fast Save never drops the selection.
  const readingRef = useRef(false)
  const [reading, setReading] = useState(false)
  const [readFailed, setReadFailed] = useState(false)
  const submit = () => { if (!readingRef.current) save.mutate() }
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
    if (readingRef.current) return
    setHint('')
    setReadFailed(false)
    readingRef.current = true
    setReading(true)
    try {
      const base64 = await fileToBase64(file)
      setClear(false)
      setPicture({ base64, preview: avatarPreviewUri(file.type, base64) })
    } catch {
      setReadFailed(true)
    } finally {
      readingRef.current = false
      setReading(false)
    }
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
          <Btn primary onClick={submit} disabled={save.isPending || reading} data-testid="person-edit-save">
            {save.isPending ? i18nT('sharedCrew.edit_saving') : i18nT('sharedCrew.edit_save')}
          </Btn>
        </div>
      )}
    >
      <form className="flex flex-col gap-3 min-w-0" data-testid="person-edit-dialog" onSubmit={e => { e.preventDefault(); submit() }}>
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
              <Btn type="button" onClick={() => fileRef.current?.click()} disabled={save.isPending || reading} aria-describedby={hint ? 'person-edit-hint' : undefined}>{i18nT('sharedCrew.edit_picture')}</Btn>
              {shown && (
                <Btn type="button" onClick={() => { setPicture(null); setClear(true) }} disabled={save.isPending || reading} data-testid="person-edit-remove-picture">
                  {i18nT('sharedCrew.edit_remove_picture')}
                </Btn>
              )}
            </div>
          </div>
        </div>
        {hint && <span id="person-edit-hint" role="status" className="text-[12px] text-text" data-testid="person-edit-hint">{inviteErrorText(hint)}</span>}
        {/* No hand-off: the dialog holds the unsaved name and picture. */}
        {readFailed && <ErrorNotice variant="inline" message={inviteErrorText('error')} testId="person-edit-read-error" />}
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

/** "Invites · N": invites whose person is not a row yet, below the people
 *  they are not counted in. */
function NotJoined({ invites }: { invites: TailscaleInvite[] }) {
  if (invites.length === 0) return null
  return (
    <>
      <PanelSectionHeader label={i18nT('sharedCrew.invites_title')} count={fmtNumber(invites.length)} className="pt-3" />
      <ul className="flex flex-col divide-y divide-border" data-testid="people-waiting">
        {invites.map(invite => <InviteRow key={invite.id} invite={invite} />)}
      </ul>
    </>
  )
}

/** "People · N": everyone connected or seen, then email invites nobody has
 *  accepted yet. N counts people; an invite is not one yet. A current member
 *  hides their own invite, so each person is one row. */
export function PeopleList({ people, usageAvailable, notes = [], footer }: {
  people: PeopleRow[]
  usageAvailable: boolean
  /** States the owner must see (Tailscale was not running, no work counts), one line each. */
  notes?: string[]
  footer?: ReactNode
}) {
  const [editing, setEditing] = useState<PeopleRow | null>(null)
  const invites = useInvites()
  const all = invites.data?.invites ?? []
  const waiting = waitingInvites(people, all)
  const count = people.length
  const canRemove = !!invites.data
  return (
    <SettingsSection title={i18nT('sharedCrew.people_title', { n: fmtNumber(count) })}>
      <SettingsCard>
        {notes.map(note => <p key={note} className="text-[13px] text-muted pb-2" data-testid="people-note">{note}</p>)}
        {count + waiting.length === 0 ? (
          !invites.isLoading && (!invites.isError || inviteErrorCode(invites.error) === 'owner_only') && !invites.data?.sync?.error
            ? <p className="text-[13px] text-muted">{i18nT('sharedCrew.people_seen_empty')}</p>
            : null
        ) : (
          <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.people_title', { n: fmtNumber(count) })} data-testid="people-list">
            {people.map(row => (
              <PersonLine
                key={row.principal.id}
                row={row}
                usageAvailable={usageAvailable}
                invite={all.find(i => i.id === row.member?.invite_id)}
                canRemove={canRemove}
                onEdit={setEditing}
              />
            ))}
          </ul>
        )}
        <NotJoined invites={waiting} />
        {footer}
      </SettingsCard>
      {editing && <EditPersonDialog row={editing} onClose={() => setEditing(null)} />}
    </SettingsSection>
  )
}

/** The invite rows when there is no People list (personal mode), so an
 *  invite nobody accepted can still be resent or removed. */
export function PersonalWaiting() {
  const invites = useInvites()
  return <NotJoined invites={waitingInvites([], invites.data?.invites ?? [])} />
}

/** Whether the invite routes answered for this viewer (the owner). */
export function useCanInvite(): boolean {
  return !!useInvites().data
}
