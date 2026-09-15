/**
 * The member thread's two gallery-facing surfaces (design step 6):
 *
 * - `MemberNameEditor`: the thread header's title row. A crewmate arrives
 *   already named by its owner (the gallery's naming step is required), so the
 *   header shows that name with the role as a subtitle, and the pencil renames
 *   it IN PLACE (an input over the label; Enter saves through
 *   `PUT /api/agents/{id}` with `display_name` only, Escape cancels). No
 *   navigation to the crew editor: renaming happens where the colleague
 *   already is.
 *
 * - `MemberEmptyState`: what an empty DM thread shows instead of "Session
 *   ready": the member's one-line duty and up to three starter prompts whose
 *   **Ask** sends the prompt through the pane's own composer path (ChatPane's
 *   `emptyState` render prop hands the send in). Rendered in the message
 *   column, never as an overlay.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, FileText, Pencil } from 'lucide-react'
import { api, type HireTemplateCard, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import ErrorNotice from '../../components/ErrorNotice'
import { memberLabel } from './rosterFilter'

/** Server-side cap on a display name (member_identity.DISPLAY_NAME_MAX_LEN). */
export const MEMBER_LABEL_MAX_LEN = 80

/** The card a member was hired from, by the catalog's own attribution
 *  (`hired_as`); undefined when the template is gone or the member was made
 *  another way. Pure, so the lookup is testable without the page. */
export function cardForMember(cards: readonly HireTemplateCard[] | undefined, member: string): HireTemplateCard | undefined {
  return cards?.find((c) => c.hired_as.some((m) => m.id === member))
}

export function MemberNameEditor({ member, separator, showId = false }: { member: MemberRosterRow; separator: string; showId?: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string>('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  const rename = useMutation({
    mutationFn: (display_name: string) => api.updateKirocrewAgent(member.name, { display_name }),
    onMutate: () => setError(''),
    onSuccess: async () => {
      setEditing(false)
      // The roster row is the read path for the label: refetch before the
      // pending state ends so the header never shows the old name after a 2xx.
      await qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    },
    onError: (e: Error) => setError(e.message || t('pages.membersPage.rename_failed')),
  })

  const begin = () => {
    setDraft(memberLabel(member))
    setEditing(true)
  }
  const commit = () => {
    const next = draft.trim().replace(/\s+/g, ' ')
    if (!next || next === memberLabel(member)) { setEditing(false); return }
    rename.mutate(next)
  }

  return (
    <div className="group/title min-w-0 flex-1 flex flex-col" data-testid="member-title-row">
      <div className="min-w-0 flex items-center gap-1.5">
        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            maxLength={MEMBER_LABEL_MAX_LEN}
            disabled={rename.isPending}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); commit() }
              if (e.key === 'Escape') { e.preventDefault(); setEditing(false); setError('') }
            }}
            onBlur={commit}
            aria-label={t('pages.membersPage.rename_member')}
            className="min-w-0 flex-1 max-w-[320px] bg-bg-elevated border border-border rounded-md px-2 py-0.5 text-[13.5px] font-semibold text-text focus-ring"
            data-testid="member-rename-input"
          />
        ) : (
          <div className="min-w-0 truncate">
            <span className="text-[13.5px] font-semibold" data-testid="member-header-label">{memberLabel(member)}</span>
            {/* The id, only while another crewmate wears this label (the roster
                decides -- twinLabelIds): the destination then says which of
                the two this is, the way the picker did. */}
            {showId && (
              <span className="ml-1.5 rounded border border-border bg-bg-elevated px-1 font-mono text-[11px] font-normal text-muted align-middle" title={t('pages.membersPage.member_id')} aria-label={`${t('pages.membersPage.member_id')}: ${member.name}`} data-testid="member-header-id">{t('pages.membersPage.member_id_badge', { id: member.name })}</span>
            )}
            {/* The role is a subtitle, so it is withheld while it IS the name
                (an owner may well name a crewmate after its role): "Oncall
                Triage Engineer · Oncall Triage Engineer" says nothing twice. */}
            {member.role && member.role !== memberLabel(member) && (
              <span className="text-[12px] text-muted" data-testid="member-header-role">{separator}{member.role}</span>
            )}
          </div>
        )}
        {!editing && (
          /* Visible at rest (dimmed), full on hover or focus: the naming
             dialog promises "You can rename it later from its chat", so the
             affordance has to be findable without hovering for it. */
          <button
            type="button"
            onClick={begin}
            className="inline-flex shrink-0 items-center justify-center w-6 h-6 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer focus-ring transition-opacity duration-150 motion-reduce:transition-none focus-visible:opacity-100 opacity-60 group-hover/title:opacity-100"
            aria-label={t('pages.membersPage.rename_member')}
            title={t('pages.membersPage.rename_member')}
            data-testid="member-rename-button"
          >
            <Pencil size={13} className="lucide-inline" />
          </button>
        )}
      </div>
      {error && (
        <div className="mt-1">
          {/* No hand-off: the unsaved `draft` is still in the input beside this
              notice, the user's to retry with Enter or drop with Escape -- a
              hand-off would leave that draft behind. */}
          <ErrorNotice message={error} variant="inline" testId="member-rename-error" />
        </div>
      )}
    </div>
  )
}

export function MemberEmptyState({ member, card, catalogFailed = false, onAsk }: { member: MemberRosterRow; card?: HireTemplateCard; catalogFailed?: boolean; onAsk: (text: string) => void }) {
  const { t } = useTranslation()
  const duty = card?.duty || card?.description || ''
  const starters = (card?.starter_prompts ?? []).slice(0, 3)
  if (catalogFailed && !card) {
    // The catalog read failed: say so where the duty and starters would be,
    // rather than a "Session ready" that reads like a member with no card.
    return (
      <div className="mx-auto max-w-[560px] px-4 py-8 flex flex-col gap-4" data-testid="member-empty-state">
        <div className="text-center text-[14px] font-semibold text-text-strong">{t('pages.membersPage.empty_greeting', { name: memberLabel(member) })}</div>
        <ErrorNotice message={t('pages.membersPage.empty_catalog_error')} variant="inline" askAgent testId="member-empty-catalog-error" />
      </div>
    )
  }
  return (
    <div className="mx-auto max-w-[560px] px-4 py-8 flex flex-col gap-4" data-testid="member-empty-state">
      <div className="text-center">
        <div className="text-[14px] font-semibold text-text-strong">{t('pages.membersPage.empty_greeting', { name: memberLabel(member) })}</div>
        {(duty || member.role) && (
          <div className="mt-1 text-[13px] text-muted" data-testid="member-empty-duty">{duty || member.role}</div>
        )}
      </div>
      {starters.length > 0 && (
        <ul className="list-none m-0 p-0 flex flex-col gap-2" data-testid="member-empty-starters">
          {starters.map((s) => (
            <li key={s.text} className="flex items-center gap-3 px-3.5 py-2.5 rounded-lg border border-border bg-bg-elevated">
              <span className="flex-1 min-w-0 flex flex-col gap-1">
                <span className="text-[13px] text-text leading-snug">{s.text}</span>
                {s.attachment && (
                  <span className="inline-flex items-center gap-1 self-start text-[11px] text-muted border border-border rounded-md px-1.5 h-[20px] bg-card">
                    <FileText size={11} /> {s.attachment}
                  </span>
                )}
              </span>
              <button
                type="button"
                onClick={() => onAsk(s.text)}
                className="shrink-0 inline-flex items-center gap-1 text-[12.5px] text-accent hover:underline bg-transparent border-none cursor-pointer focus-ring rounded"
                data-testid="member-empty-ask"
              >
                {t('pages.membersPage.empty_ask')} <ArrowRight size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {starters.length === 0 && (
        <div className="text-center text-muted text-[13px]">{t('components.chatPane.session_ready_type_a_message_to_start')}</div>
      )}
    </div>
  )
}
