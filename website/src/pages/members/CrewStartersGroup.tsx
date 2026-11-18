import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { RowGroup } from './profileRow'

/** The gateway's bounds (`member_starters.py`); it enforces them, this only words the hint. */
const STARTER_MAX_COUNT = 4
const STARTER_MAX_CHARS = 120

/** One line the gateway left out: its 1-based place in the list sent, and why. */
type LeftOutPlace = { place: number; reason: string }
/** The save's answer: the starters kept, and each line left out. */
type StartersSaved = { warnings?: string[]; starters?: string[]; starters_left_out?: LeftOutPlace[] }
/** A line the last save left out, as typed, with the gateway's reason code. */
type LeftOutLine = { line: string; reason: string }

/** The reasons the editor words; a line left out for any other one is listed without a reason. */
const REASON_KEYS: Record<string, string> = {
  too_long: 'pages.membersPage.profile_starters_reason_too_long',
  repeated: 'pages.membersPage.profile_starters_reason_repeated',
  over_limit: 'pages.membersPage.profile_starters_reason_over_limit',
  credential: 'pages.membersPage.profile_starters_reason_credential',
}

/** The lines the box holds as the gateway will receive them: trimmed, none blank. */
const draftLines = (text: string) => text.split('\n').map((line) => line.trim()).filter(Boolean)
const joinedLines = (text: string) => draftLines(text).join('\n')

/**
 * The Bot's conversation starters, one per line: the prompts its empty chat offers as
 * buttons. The gateway keeps what passes its bounds and names the place of each line it
 * left out and why, so a line that is too long or repeated never blocks the rest from
 * saving; the box then shows what was kept, and the note under Save says how many were
 * saved and gives each line that went its own reason.
 */
export default function CrewStartersGroup({ member, name, onDraftChange }: {
  member: MemberRosterRow
  name: string
  /** The page's leave guards: a draft not yet saved, and a save in flight. */
  onDraftChange?: (dirty: boolean, busy: boolean) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const savedText = (member.starters ?? []).join('\n')
  const [text, setText] = useState(savedText)
  // What the last save kept and left out, in the order typed; null when it left out none.
  const [leftOut, setLeftOut] = useState<{ saved: number; lines: LeftOutLine[] } | null>(null)
  // The list the server last confirmed: the first load, a roster read that brought a new
  // list, or this editor's own save answer. Dirty is judged against it, not against the
  // roster props, which can still hold the old list while a refetch is on its way.
  const [acknowledged, setAcknowledged] = useState(savedText)
  // The roster props as last seen, so a re-render with the same props changes nothing.
  const lastSeen = useRef(savedText)
  // The saved list changed elsewhere while this box held an unsaved draft.
  const [changedElsewhere, setChangedElsewhere] = useState(false)
  // A change from elsewhere (another window, an applied card) reaches the box while the
  // owner has typed nothing; a draft is kept, with a note that the saved list moved under it.
  // Props that equal the last ones seen, or the acknowledged list, are not news.
  useEffect(() => {
    if (savedText === lastSeen.current) return
    lastSeen.current = savedText
    if (savedText === acknowledged) return
    const typed = joinedLines(text)
    setAcknowledged(savedText)
    if (typed === acknowledged || typed === savedText) setText(savedText)
    else setChangedElsewhere(true)
  }, [savedText, text, acknowledged])

  const save = useMutation({
    mutationFn: (starters: string[]) => api.updateKirocrewAgent(member.name, { starters }) as Promise<StartersSaved>,
    onSuccess: (result, sent) => {
      // The box shows what was saved, even when that equals the starters it already had.
      const kept = Array.isArray(result?.starters) ? result.starters : null
      if (kept) {
        setText(kept.join('\n'))
        setAcknowledged(kept.join('\n'))
      }
      setChangedElsewhere(false)
      const lines = (result?.starters_left_out ?? []).flatMap(({ place, reason }) => {
        const line = sent[place - 1]
        return typeof line === 'string' ? [{ line, reason }] : []
      })
      setLeftOut(lines.length > 0 ? { saved: kept?.length ?? 0, lines } : null)
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }),
  })
  const lines = draftLines(text)
  const dirty = lines.join('\n') !== acknowledged
  // The note describes a draft; once the box matches the saved list again it has nothing to say.
  useEffect(() => { if (!dirty) setChangedElsewhere(false) }, [dirty])
  // Published so every exit that unmounts this box asks first about an unsaved draft and
  // waits for a save in flight, as it does for the Goals and Export drafts.
  const busy = save.isPending
  useEffect(() => { onDraftChange?.(dirty, busy) }, [dirty, busy, onDraftChange])
  useEffect(() => () => onDraftChange?.(false, false), [onDraftChange])

  return (
    <>
      <RowGroup caption={t('pages.membersPage.profile_starters_hint', { name, max: STARTER_MAX_COUNT, chars: STARTER_MAX_CHARS })}>
        <div className="flex flex-col gap-2 px-3.5 py-3" data-testid="crew-profile-starters">
          <label className="text-[13px] font-semibold text-text" htmlFor="crew-profile-starters-input">
            {t('pages.membersPage.profile_starters_title')}
          </label>
          <textarea
            id="crew-profile-starters-input"
            aria-label={t('pages.membersPage.profile_starters_title')}
            value={text}
            rows={4}
            disabled={busy}
            onChange={(e) => { setText(e.target.value); setLeftOut(null) }}
            className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
            data-testid="crew-profile-starters-input"
          />
          <div className="flex justify-end">
            <Btn
              type="button"
              primary
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate(lines)}
              data-testid="crew-profile-starters-save"
            >
              {t('pages.membersPage.profile_starters_save')}
            </Btn>
          </div>
          {changedElsewhere && dirty && (
            <p className="text-[12.5px] leading-snug text-warn-text" role="status" data-testid="crew-profile-starters-changed-elsewhere">
              {t('pages.membersPage.profile_starters_changed_elsewhere')}
            </p>
          )}
          {leftOut && (
            <div className="text-[12.5px] leading-snug text-warn-text" role="status" data-testid="crew-profile-starters-left-out">
              <p>{t('pages.membersPage.profile_starters_left_out', { count: leftOut.saved })}</p>
              <ul className="mt-1 list-disc pl-5">
                {leftOut.lines.map(({ line, reason }, index) => (
                  <li key={index} className="break-words" data-testid="crew-profile-starters-left-out-line">
                    {REASON_KEYS[reason]
                      ? t(REASON_KEYS[reason], { max: STARTER_MAX_COUNT, chars: STARTER_MAX_CHARS, line })
                      : line}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </RowGroup>
      {/* No hand-off: the starter draft in the box above is not saved when Save
          fails, so a navigation to the chat would discard it. */}
      <ErrorNotice message={save.isError ? t('pages.membersPage.profile_starters_failed') : null} testId="crew-profile-starters-error" />
    </>
  )
}
