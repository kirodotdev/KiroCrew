import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type CrewTeam, type MemberRosterRow } from '../../api/client'
import { TEAMS_QUERY_KEY, withSavedTeam } from '../../api/teamsQuery'
import Modal from '../../components/Modal'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Checkbox, Input } from '../../components/ui'
import { findReport } from '../../utils/errorReport'
import { teamOfMember } from './teamGroups'

/**
 * New team / Edit team. One dialog, two modes: `team` set means edit (name and
 * checklist prefilled, a delete affordance at the foot of the list), unset
 * means create.
 *
 * The checklist lists EVERY crewmate with its current team as muted text, and
 * the helper line says what picking one does: a crewmate is on one team at a
 * time, so ticking it here moves it out of its current team (the store
 * enforces that in the same write; the UI only has to say so).
 *
 * Errors render INSIDE the dialog, under the fields, through the shared
 * notice; every dismissal is refused visibly while a write is in flight
 * (`dismissDisabled` + disabled Cancel), so a save cannot be abandoned
 * half-way by a stray Escape.
 */
export default function TeamDialog({
  open,
  team,
  teams,
  members,
  onClose,
  onSaved,
  onDeleted,
}: {
  open: boolean
  /** The team being edited; `undefined` creates a new one. */
  team?: CrewTeam
  teams: readonly CrewTeam[]
  /** Every roster row, in the roster's display order. */
  members: readonly MemberRosterRow[]
  onClose: () => void
  onSaved: (team: CrewTeam) => void
  onDeleted?: (team: CrewTeam) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const editing = !!team
  const [name, setName] = useState(team?.name ?? '')
  const [picked, setPicked] = useState<Set<string>>(() => new Set(team?.members ?? []))
  // Unsaved input: a typed name or a changed pick. While it exists the
  // ACCIDENTAL dismissals (Escape, a backdrop click) are ignored; Cancel and
  // the X still close, so a deliberate exit is one click as before.
  const initialPicked = team?.members ?? []
  const trimmed = name.trim()
  const nameChanged = trimmed !== (team?.name ?? '')
  // The crewmates THIS dialog toggled, against the list it opened with. Only
  // these travel on an edit (as add / remove deltas the route applies to the
  // list as it stands), so a second tab's newer membership is never overwritten.
  const initialSet = new Set(initialPicked)
  const added = members.filter((m) => picked.has(m.name) && !initialSet.has(m.name)).map((m) => m.name)
  const removed = initialPicked.filter((n) => !picked.has(n))
  const membersChanged = added.length > 0 || removed.length > 0
  const dirty = name !== (team?.name ?? '') || membersChanged
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorKind, setErrorKind] = useState<'save' | 'delete' | null>(null)

  const toggle = (crewmate: string) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(crewmate)) next.delete(crewmate)
      else next.add(crewmate)
      return next
    })

  // Members are sent in the ROSTER's order so the stored list is the order the
  // user saw when picking; the roster re-sorts them for display anyway.
  const orderedPicks = () => members.filter((m) => picked.has(m.name)).map((m) => m.name)

  const save = useMutation({
    mutationFn: async (): Promise<CrewTeam> => {
      if (!team) {
        const res = await api.teams.create({ name: trimmed, members: orderedPicks() })
        return res.team
      }
      // Edit sends ONLY what this dialog changed: the name if it differs, and
      // membership as add / remove deltas rather than the whole list. The route
      // leaves an omitted field alone and applies a delta to the list as it
      // stands, so a dialog opened before another tab moved a crewmate can
      // neither rename that tab's membership away nor write its own stale
      // snapshot back over it.
      const body: { name?: string; add?: string[]; remove?: string[] } = {}
      if (nameChanged) body.name = trimmed
      if (added.length > 0) body.add = added
      if (removed.length > 0) body.remove = removed
      const res = await api.teams.update(team.id, body)
      return res.team
    },
    onSuccess: async (saved) => {
      setError(null)
      setErrorKind(null)
      // The route answered with the team as it now is: write that into the
      // cache FIRST, so the roster shows the saved team even if the refetch
      // the invalidation starts fails (which the page then says). The merge
      // also drops the saved members from every other cached team -- the
      // store moved them in the same write, and a cache that still lists a
      // moved crewmate on its old team would survive a failed refetch.
      queryClient.setQueryData<CrewTeam[]>(TEAMS_QUERY_KEY, (prev) => withSavedTeam(prev, saved))
      await queryClient.invalidateQueries({ queryKey: TEAMS_QUERY_KEY })
      onSaved(saved)
    },
    onError: (err) => {
      setErrorKind('save')
      setError(err instanceof Error ? err.message : String(err))
    },
  })

  const remove = useMutation({
    mutationFn: async (): Promise<CrewTeam> => {
      if (!team) throw new Error('no team')
      await api.teams.remove(team.id)
      return team
    },
    onSuccess: async (removed) => {
      setError(null)
      setErrorKind(null)
      // Same order as a save: the cache drops the team before the refetch.
      queryClient.setQueryData<CrewTeam[]>(TEAMS_QUERY_KEY, (prev) =>
        prev === undefined ? prev : prev.filter((tm) => tm.id !== removed.id),
      )
      await queryClient.invalidateQueries({ queryKey: TEAMS_QUERY_KEY })
      onDeleted?.(removed)
    },
    onError: (err) => {
      setErrorKind('delete')
      setError(err instanceof Error ? err.message : String(err))
    },
  })

  const busy = save.isPending || remove.isPending
  // In edit mode an unchanged team has nothing to send (the route refuses an
  // empty update), so Save stays disabled until a field actually differs.
  const canSave = trimmed.length > 0 && !busy && (!editing || nameChanged || membersChanged)
  const title = t(editing ? 'pages.membersPage.team_edit' : 'pages.membersPage.team_new')
  const errorTitle = t(
    errorKind === 'delete'
      ? 'pages.membersPage.team_delete_failed'
      : editing
        ? 'pages.membersPage.team_save_failed'
        : 'pages.membersPage.team_create_failed',
  )

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      maxWidth={440}
      guardAccidentalDismiss={dirty}
      dismissDisabled={busy}
      footer={(
        <>
          <Btn type="button" onClick={onClose} disabled={busy} data-testid="team-dialog-cancel">
            {t('pages.membersPage.cancel')}
          </Btn>
          <Btn
            type="button"
            primary
            disabled={!canSave}
            onClick={() => save.mutate()}
            data-testid="team-dialog-save"
          >
            {t(editing ? 'pages.membersPage.team_save' : 'pages.membersPage.team_create')}
          </Btn>
        </>
      )}
    >
      <form
        className="flex flex-col gap-4"
        data-testid="team-dialog-body"
        onSubmit={(e) => {
          e.preventDefault()
          if (canSave) save.mutate()
        }}
      >
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] font-medium text-muted">{t('pages.membersPage.team_name_label')}</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('pages.membersPage.team_name_placeholder')}
            autoFocus
            maxLength={80}
            disabled={busy}
            data-testid="team-dialog-name"
          />
        </label>
        <div className="flex flex-col gap-1.5">
          <span className="text-[12px] font-medium text-muted">{t('pages.membersPage.team_members_label')}</span>
          {members.length === 0 ? (
            <p className="m-0 text-[12px] text-muted" data-testid="team-dialog-empty">
              {t('pages.membersPage.empty_roster')}
            </p>
          ) : (
            <ul
              className="m-0 p-0 list-none rounded-md border border-border divide-y divide-border max-h-[40vh] overflow-y-auto"
              data-testid="team-dialog-list"
            >
              {members.map((m) => {
                const current = teamOfMember(teams, m.name)
                // Its OWN team is not "current elsewhere": in edit mode the
                // team being edited reads as "on this team", not as a move.
                // "On Docs", not a bare "Docs": a bare team name beside a
                // crewmate's name read as a job title to a first-time reader.
                const currentLabel =
                  current && current.id !== team?.id
                    ? t('pages.membersPage.team_on_team', { team: current.name })
                    : t('pages.membersPage.team_none')
                return (
                  <li key={m.name}>
                    <label
                      className="flex items-center gap-2.5 px-3 py-2 cursor-pointer hover:bg-bg-hover transition-colors"
                      data-testid="team-dialog-row"
                    >
                      <Checkbox
                        checked={picked.has(m.name)}
                        onChange={() => toggle(m.name)}
                        disabled={busy}
                        aria-label={m.name}
                      />
                      <CrewAvatar seed={m.name} avatar={m.avatar} size={24} />
                      <span className="text-[13px] font-medium text-text flex-1 truncate">{m.name}</span>
                      <span className="text-[11.5px] text-muted truncate">
                        {current && current.id === team?.id ? t('pages.membersPage.team_on_this_team') : currentLabel}
                      </span>
                    </label>
                  </li>
                )
              })}
            </ul>
          )}
          <p className="m-0 text-[11.5px] text-muted leading-snug">{t('pages.membersPage.team_one_team_hint')}</p>
        </div>
        {/* The "Delete team" label stays where it was when the confirmation
            appears beside it, so the row reads as one continuing action --
            "Delete team ... Delete this team? [Keep team] [Delete <name>]" --
            not as a different control set swapped into its place. While
            confirming, the anchor is muted text, not a button, and the one
            armed control names the team it deletes, so two red "Delete team"
            labels never sit side by side asking which one is live. */}
        {editing && (
          <div className="flex items-center gap-2 min-h-7 flex-wrap" data-testid="team-dialog-delete-row">
            {confirmDelete ? (
              <span className="text-[12px] text-muted shrink-0" data-testid="team-dialog-delete-anchor">
                {t('pages.membersPage.team_delete')}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                disabled={busy}
                className="text-[12px] text-danger hover:underline bg-transparent border-none cursor-pointer p-0"
                data-testid="team-dialog-delete"
              >
                {t('pages.membersPage.team_delete')}
              </button>
            )}
            {confirmDelete && (
              <>
                <span className="text-[12px] text-muted flex-1 min-w-[8rem]">{t('pages.membersPage.team_delete_confirm')}</span>
                <Btn type="button" onClick={() => setConfirmDelete(false)} disabled={busy} data-testid="team-dialog-delete-keep">
                  {t('pages.membersPage.team_delete_keep')}
                </Btn>
                <Btn type="button" danger onClick={() => remove.mutate()} disabled={busy} data-testid="team-dialog-delete-confirm">
                  {t('pages.membersPage.team_delete_named', { name: team?.name ?? '' })}
                </Btn>
              </>
            )}
          </div>
        )}
        {/* No hand-off: the name and the picks above are an unsaved draft. */}
        <ErrorNotice
          message={error}
          report={findReport(error ?? undefined)}
          title={errorTitle}
          onDismiss={() => {
            setError(null)
            setErrorKind(null)
          }}
          testId="team-dialog-error"
        />
      </form>
    </Modal>
  )
}
