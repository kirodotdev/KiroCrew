/**
 * One line in a crewmate's chat for a change it made to itself: its name, its
 * description or its face (`update_self`, the `kirocrew-mate` server).
 *
 * The gateway writes the record the moment the crewmate asks, so there is no
 * card to press -- the row tells the user what just changed and offers one
 * **Undo**. Drawn centred and quiet, like a notice, with the old face beside the
 * new one when the face changed. The row's `before`/`after` are for drawing
 * only: Undo posts to the gateway, which restores from its OWN record of the
 * change (`dashboard/member_self_changes.py`) and then marks this row
 * `undone` over the same `chat_message_update` frame every card row uses.
 */
import { useState } from 'react'
import { ArrowRight, RotateCcw } from 'lucide-react'
import { apiTransport } from '../api/apiTransport'
import { apiErrorCode } from '../api/apiError'
import { fmtList } from '../i18n/format'
import CrewAvatar from '../components/CrewAvatar'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'

export type SelfField = 'display_name' | 'description' | 'avatar'

export interface SelfChangeRef {
  surface: 'self'
  id: string
  slot: string
  /** The crewmate's agent key (the avatar seed), never shown. */
  member: string
  /** The display name the crewmate goes by after the change: what the row calls it. */
  name: string
  /** The crewmate's slug, which the undo route is addressed to. */
  slug: string
  /** The fields that changed, in display order. */
  fields: SelfField[]
  before: Partial<Record<SelfField, unknown>>
  after: Partial<Record<SelfField, unknown>>
  status: 'applied' | 'undone'
  /** False when the gateway kept no undo record for this change (a retained value was over its bound). */
  undoable: boolean
}

const FACE_PX = 22

const str = (v: unknown, max = 300) => (typeof v === 'string' ? v.slice(0, max) : '')

/** The user's word for each field a crewmate can change about itself. */
const FIELD_WORDS: Record<SelfField, () => string> = {
  display_name: () => i18nT('components.selfChange.field_display_name'),
  description: () => i18nT('components.selfChange.field_description'),
  avatar: () => i18nT('components.selfChange.field_avatar'),
}

function titleFor(ref: SelfChangeRef): string {
  // The row keeps one name for its whole life: the one the crewmate went by
  // once the change was applied (a rename's NEW name). An undo adds "· undone"
  // and does not reword the row, so it still reads as the same note.
  const name = str(ref.after.display_name) || ref.name || ref.member
  const field = ref.fields.length === 1 ? ref.fields[0] : null
  if (field === 'avatar') return i18nT('components.selfChange.changed_avatar', { name })
  if (field === 'description') return i18nT('components.selfChange.changed_description', { name })
  if (field === 'display_name') return i18nT('components.selfChange.changed_display_name', { name })
  // Several fields at once: name them, so the user knows what one Undo reverses.
  return i18nT('components.selfChange.changed_fields', { name, fields: fmtList(ref.fields.map(f => FIELD_WORDS[f]())) })
}

/** `slot` is the conversation the page is showing, from the page's own state:
 *  the row is transcript data the agent can write, so the gateway binds an undo
 *  to the conversation it was pressed in, not to what the row says about itself.
 *  Without one the gateway refuses, so the row offers no Undo. */
export default function SelfChangeRow({ change, slot }: { change: SelfChangeRef; slot?: string | null }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set once the gateway answered that it holds no record: the button would
  // only fail the same way again, so it goes and the message stays.
  const [gone, setGone] = useState(false)
  const undone = change.status === 'undone'
  const faceChanged = change.fields.includes('avatar')
  const undo = async () => {
    setPending(true)
    setError(null)
    try {
      const { post, j } = apiTransport
      // `j` throws an ApiError on a non-2xx answer; the gateway's refusal code
      // rides in its body, so a refusal is read in the catch below.
      await post(
        `/api/members/${encodeURIComponent(change.slug)}/self-changes/${encodeURIComponent(change.id)}/undo`,
        { slot },
      ).then(j)
      // The row itself flips to `undone` through the gateway's row update; no
      // local state to keep in step.
    } catch (e) {
      const code = apiErrorCode(e)
      // Both refusals are final for this row: the gateway holds no record
      // (restart, or past the newest changes), or the field was edited again
      // since. The button goes; the message stays and names the crew editor.
      if (code === 'undo_unavailable' || code === 'stale_undo') setGone(true)
      // The way to the profile is spelled out under the crewmate's current
      // name: the row is the only thing on screen, and no pencil sits beside it.
      const name = str(change.after.display_name) || change.name || change.member
      setError(code === 'undo_unavailable'
        ? i18nT('components.selfChange.undo_unavailable', { name })
        : code === 'stale_undo'
          ? i18nT('components.selfChange.undo_stale', { name })
          : i18nT('components.selfChange.undo_failed', { message: e instanceof Error ? e.message : String(e) }))
    } finally {
      setPending(false)
    }
  }
  return (
    <div className="flex flex-col items-center gap-1.5" data-testid="self-change-row" data-self-status={change.status}>
      <div
        className={`inline-flex max-w-full items-center gap-2.5 rounded-full border border-border bg-bg-elevated px-3 py-1.5 text-[12px] text-muted ${undone ? 'opacity-70' : ''}`}
      >
        {faceChanged && (
          <span className="inline-flex items-center gap-1.5" aria-hidden="true">
            <span className={undone ? '' : 'opacity-60'}>
              <CrewAvatar seed={change.member} avatar={change.before.avatar} size={FACE_PX} />
            </span>
            <ArrowRight size={12} className="lucide-inline text-muted-strong" />
            <span className={undone ? 'opacity-60' : ''}>
              <CrewAvatar seed={change.member} avatar={change.after.avatar} size={FACE_PX} />
            </span>
          </span>
        )}
        <span className="min-w-0 truncate">
          {titleFor(change)}
          {undone && <span className="ml-1">· {i18nT('components.selfChange.undone')}</span>}
        </span>
        {!undone && change.undoable && !gone && !!slot && (
          <button
            type="button"
            onClick={() => void undo()}
            disabled={pending}
            aria-busy={pending || undefined}
            className="inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-accent hover:bg-accent-subtle disabled:opacity-50"
            data-testid="self-change-undo"
          >
            <RotateCcw size={12} className="lucide-inline" aria-hidden="true" />
            {i18nT('components.selfChange.undo')}
          </button>
        )}
      </div>
      {/* No hand-off: the row sits in the chat beside the composer, whose unsent draft the hand-off navigation would discard. */}
      {error && <ErrorNotice variant="inline" testId="self-change-undo-error" message={error} />}
    </div>
  )
}
