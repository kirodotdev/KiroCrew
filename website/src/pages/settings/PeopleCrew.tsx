/** Settings > People: the one People list, below the connection sources
 *  (`PeopleSources.tsx`).
 *
 *  Each row carries a chip naming the source the person came in through
 *  (`sharedCrewUi.personSource`), so the same person can be a row once per
 *  source; the heading counts those rows, so its number is the rows a person
 *  sees. Invites nobody has used yet are rows of the same list, with their
 *  source chip, and are not counted.
 *
 *  Remove sits where it works: an SSM person and an unopened SSM invite
 *  ("Revoke invite"), and a person or invite of a single-use Tailscale invite.
 *  Any other Tailscale person links to Tailscale's users page ("Manage in
 *  Tailscale"); a person your organization manages reads "Managed by admin".
 *
 *  Any admitted person can edit any person's display name and picture (the ✎ on
 *  each row with a known Tailscale user id). It is display only: the verified
 *  login stays on the row, and who can connect is decided by the source. */
import { useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Pencil } from 'lucide-react'
import { api } from '../../api/client'
import { AVATAR_MAX_BYTES, AVATAR_TYPES, avatarPreviewUri, avatarUrl, fileToBase64, inviteErrorCode } from '../../api/sharedCrewInvites'
import { ssmInviteFor, waitingSsmInvites, type SsmInvite } from '../../api/sharedCrewSsm'
import { personSource, principalInitials, principalName, waitingInvites, type PeopleRow, type PersonSource } from '../../api/sharedCrewUi'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { TAILSCALE_USERS_URL } from './PeopleModes'
import { SourceChip } from './SharedAccessSetup'
import type { TailscaleInvite } from '../../api/sharedCrewInvites'
import { SsmInviteRow, useSsmStatus } from './SsmInvite'
import { InviteRow, RevokeControl, inviteErrorText, useInvites, useOwnerRead } from './TailscaleInvites'

export { useOwnerRead }

/** The owner's row before anyone names them carries only this machine's name (a
 *  hostname such as an EC2 private DNS name), which reads as a stranger. */
function ownerKnownOnlyByMachine(row: PeopleRow): boolean {
  return !!row.is_owner && !row.member?.alias && !row.principal.display.login && !!principalName(row.principal)
}

function displayName(row: PeopleRow): string {
  if (ownerKnownOnlyByMachine(row)) return i18nT('sharedCrew.crew_owner')
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

/** The link to Tailscale's users page, where a Tailscale person is managed. */
function ManageInTailscale({ children }: { children?: ReactNode }) {
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

/** The row's one way to remove or manage the person, by where it works:
 *  - an SSM person: Remove, which revokes the invite they opened;
 *  - a person who came in by a single-use Tailscale invite: Remove, which
 *    revokes it. A reusable link admitted many people, so revoking it would
 *    remove them all, and such a person is managed in Tailscale instead;
 *  - any other Tailscale person: Manage in Tailscale, for the owner (*canManage*);
 *  - a person your organization manages: "Managed by admin", read-only;
 *  - anyone else, the owner and the viewer: nothing to remove.
 *  *children* (the row's ✎) stays beside it, and hides while a confirm shows. */
function PersonAction({ row, source, name, invite, ssmInvite, canManage, children }: {
  row: PeopleRow
  source: ReturnType<typeof personSource>
  name: string
  invite: TailscaleInvite | undefined
  ssmInvite: SsmInvite | undefined
  canManage: boolean
  children?: ReactNode
}) {
  if (source === 'organization') {
    return <><span className="text-[13px] text-muted" data-testid="person-managed">{i18nT('sharedCrew.managed_by_admin')}</span>{children}</>
  }
  if (row.is_owner || row.is_viewer) return <>{children}</>
  if (ssmInvite) return <RevokeControl inviteId={ssmInvite.id} name={name} transport="ssm" state="member">{children}</RevokeControl>
  if (!canManage) return <>{children}</>
  if (invite && !invite.multi_use) return <RevokeControl inviteId={invite.id} name={name} state={row.member?.invite_id ? 'member' : 'sent'}>{children}</RevokeControl>
  // Only someone Tailscale shares this machine with is managed there. A session
  // creator, a machine, or a person on another source has nothing to manage.
  if (source === 'tailscale' || row.member?.invite_id || invite) return <ManageInTailscale>{children}</ManageInTailscale>
  return <>{children}</>
}

/** One person. *invite* (Tailscale) or *ssmInvite* is the invite they came in by,
 *  known only to the owner, who is also the only one offered a way to remove
 *  them (*canManage*, or the SSM invite itself). */
function PersonLine({ row, usageAvailable, invite, ssmInvite, canManage, fallbackSource, onEdit }: {
  row: PeopleRow
  usageAvailable: boolean
  invite: TailscaleInvite | undefined
  ssmInvite: SsmInvite | undefined
  canManage: boolean
  fallbackSource: PersonSource | null
  onEdit: (row: PeopleRow) => void
}) {
  const name = displayName(row)
  // The machine name stays as the detail line when it is all the owner row has.
  const login = ownerKnownOnlyByMachine(row) ? principalName(row.principal) : row.principal.display.login
  const activity = activityText(row, usageAvailable)
  const showLogin = !!login && login !== name
  const source = personSource(row, fallbackSource)
  // Remove reads the provenance the server derived. A chip taken from the one
  // source that is on labels the row; only "Managed by admin" may follow it.
  const actionSource = source === 'organization' ? source : personSource(row)
  const edit = row.tailscale_user_id ? (
    <Btn onClick={() => onEdit(row)} aria-label={i18nT('sharedCrew.edit_person', { name })} data-testid="person-edit">
      <Pencil size={14} aria-hidden />
    </Btn>
  ) : null
  // Tailscale lets an invite be accepted from any account: say so when the
  // person who got in is not the address the owner invited.
  const otherAccount = invite?.email && login && invite.email.trim().toLowerCase() !== login.trim().toLowerCase() ? invite.email : ''
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`person-${row.principal.id}`}>
      <Avatar row={row} />
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {row.is_viewer && row.is_owner
            ? <Badge variant="muted">{i18nT('sharedCrew.you_host')}</Badge>
            : row.is_viewer
              ? <Badge variant="muted">{i18nT('sharedCrew.you')}</Badge>
              : row.is_owner && !ownerKnownOnlyByMachine(row) && <Badge variant="muted">{i18nT('sharedCrew.crew_owner')}</Badge>}
          {row.principal.kind === 'machine' && <Badge variant="muted">{i18nT('sharedCrew.kind_machine')}</Badge>}
          {source && <SourceChip id={source} />}
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
      <PersonAction row={row} source={actionSource} name={name} invite={invite} ssmInvite={ssmInvite} canManage={canManage}>{edit}</PersonAction>
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
  // Save sends the form of the render it was pressed in: `reading` drops a
  // submit from a render that still shows the read after the ref has cleared,
  // and the form is passed to mutate() because useMutation installs a new
  // mutationFn only in a passive effect, after the render that enables Save.
  const submit = () => { if (!readingRef.current && !reading) save.mutate({ alias, picture, clear }) }
  const save = useMutation({
    mutationFn: (form: { alias: string; picture: typeof picture; clear: boolean }) => api.sharedCrewSetPersonDisplay(row.tailscale_user_id ?? '', {
      alias: form.alias,
      ...(form.picture ? { avatar_data_base64: form.picture.base64 } : {}),
      ...(form.clear && !form.picture ? { clear_avatar: true } : {}),
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

/** Invites nobody has used yet, as rows of the People list with their source
 *  chip. They are not people yet, so the heading does not count them. */
function InviteRows({ invites, ssmInvites }: { invites: TailscaleInvite[]; ssmInvites: SsmInvite[] }) {
  return (
    <>
      {invites.map(invite => <InviteRow key={invite.id} invite={invite} chip={<SourceChip id="tailscale" />} />)}
      {ssmInvites.map(invite => <SsmInviteRow key={invite.id} invite={invite} chip={<SourceChip id="ssm" />} />)}
    </>
  )
}

/** "People · N": everyone connected or seen, each with the source they came in
 *  through, then invites nobody has used yet. N counts the people rows: one
 *  person on two sources is two rows, which the note under the heading
 *  explains, and an invite is not one yet. A current member hides their own invite. *fallbackSource*: the chip a
 *  row with no provenance takes (`sharedCrewMode.rowSourceFallback`). */
export function PeopleList({ people, usageAvailable, notes = [], footer, fallbackSource = null }: {
  people: PeopleRow[]
  usageAvailable: boolean
  /** States the owner must see (Tailscale was not running, no work counts), one line each. */
  notes?: string[]
  footer?: ReactNode
  fallbackSource?: PersonSource | null
}) {
  const [editing, setEditing] = useState<PeopleRow | null>(null)
  const invites = useInvites()
  const all = invites.data?.invites ?? []
  const waiting = waitingInvites(people, all)
  const ssm = useSsmStatus()
  const ssmInvites = ssm.data?.invites ?? []
  const waitingSsm = waitingSsmInvites(people, ssmInvites)
  const count = people.length
  const canManage = !!invites.data
  const title = i18nT('sharedCrew.people_title', { n: fmtNumber(count) })
  return (
    <SettingsSection title={title}>
      <SettingsCard>
        <p className="text-[13px] text-muted pb-2 m-0" data-testid="people-list-note">{i18nT('sharedCrew.people_list_note')}</p>
        {notes.map(note => <p key={note} className="text-[13px] text-muted pb-2" data-testid="people-note">{note}</p>)}
        {people.length + waiting.length + waitingSsm.length === 0 ? (
          !invites.isLoading && (!invites.isError || inviteErrorCode(invites.error) === 'owner_only') && !invites.data?.sync?.error
            ? <p className="text-[13px] text-muted">{i18nT('sharedCrew.people_seen_empty')}</p>
            : null
        ) : (
          <ul className="flex flex-col divide-y divide-border" aria-label={title} data-testid="people-list">
            {people.map(row => (
              <PersonLine
                key={row.principal.id}
                row={row}
                usageAvailable={usageAvailable}
                invite={all.find(i => i.id === row.member?.invite_id)}
                ssmInvite={ssmInviteFor(row, ssmInvites)}
                canManage={canManage}
                fallbackSource={fallbackSource}
                onEdit={setEditing}
              />
            ))}
            <InviteRows invites={waiting} ssmInvites={waitingSsm} />
          </ul>
        )}
        {footer}
      </SettingsCard>
      {editing && <EditPersonDialog row={editing} onClose={() => setEditing(null)} />}
    </SettingsSection>
  )
}

/** The invite rows when there is no People list (a Crew that cannot say who
 *  people are), so an invite nobody accepted can still be resent or removed,
 *  over either path. */
export function PersonalWaiting() {
  const invites = useInvites()
  const ssm = useSsmStatus()
  const waiting = waitingInvites([], invites.data?.invites ?? [])
  const waitingSsm = waitingSsmInvites([], ssm.data?.invites ?? [])
  if (waiting.length + waitingSsm.length === 0) return null
  return (
    <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.invites_title')} data-testid="people-waiting">
      <InviteRows invites={waiting} ssmInvites={waitingSsm} />
    </ul>
  )
}
