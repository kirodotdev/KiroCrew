/**
 * CrewAvatarButton — a crew face that visibly invites editing.
 *
 * Every face that opens the avatar builder is wrapped in this one component,
 * so the "this can be changed" signal is defined once and cannot drift between
 * the crew editor's header, its overview hub, its Avatar field, and the Crew
 * Members page (issue #9103: the two faces that DID open the builder looked
 * identical to the seven that did not, and the only text route was filed under
 * the Triggers pane).
 *
 * The face itself is the host's `children` — a `CrewAvatar` or a reactive
 * `CrewStateAvatar`, whichever that surface already rendered — so wrapping a
 * face changes nothing about how it draws or reacts; this component only adds
 * the button semantics and the affordance layers around it.
 *
 * The affordance has three layers, because no single one reaches every user:
 *
 * - **Hover / focus scrim** — a translucent theme-bg veil with a pencil glyph
 *   fades in over the face (150ms, and the global reduced-motion rule in
 *   index.css collapses it to a cut). Reaches mouse and keyboard users.
 * - **Persistent corner badge** — under `(hover: none)` the scrim can never
 *   show, so a small accent pencil sits on the bottom-right corner permanently
 *   (same escape hatch as `utils/touchActions`). Reaches touch users.
 * - **Text** — `aria-label` + `title` carry the same "Edit avatar" copy the
 *   explicit text buttons use, so the face and the button read as one action.
 *
 * The optional `hint` chip is the first-run nudge: rendered beside the face
 * while the crew still wears its name-derived default AND the user has never
 * dismissed it. Clicking either the face or the chip dismisses it for good
 * (localStorage via `usePersistedBool`; the origin already partitions storage
 * per gateway home, and the frontend has no finer workspace identity to key
 * on).
 */
import { useCallback, type ReactNode } from 'react'
import { Pencil } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { usePersistedBool } from '../../hooks/usePersistedBool'

/** One flag, not per-crew: the lesson "faces are editable" is learned once.
 *  Module-internal — no other surface reads or writes it; tests pin the
 *  literal key so a rename here cannot silently orphan stored dismissals.
 *  Persisted through `usePersistedBool` (quota-defensive write, cross-instance
 *  sync), so dismissing the chip on one face also clears a sibling's. */
const AVATAR_HINT_DISMISSED_KEY = 'mc-avatar-edit-hint-dismissed'

export interface CrewAvatarButtonProps {
  /** The face — the same `CrewAvatar` / `CrewStateAvatar` element the host
   *  rendered before it became clickable. */
  children: ReactNode
  /** Rendered edge length of that face in px; sizes the pencil glyph. */
  size: number
  /** The ONE open path. Every host passes the same function it uses for its
   *  text button, so the face and the button can never diverge. */
  onEdit: () => void
  disabled?: boolean
  /** Show the first-run "Edit this avatar" chip beside the face. The
   *  component still hides it once dismissed; hosts only say whether the
   *  face is currently the default one. */
  hint?: boolean
  'data-testid'?: string
}

export default function CrewAvatarButton({
  children, size,
  onEdit, disabled, hint,
  'data-testid': testId,
}: CrewAvatarButtonProps) {
  const { t } = useTranslation()
  const label = t('components.avatarBuilder.edit_avatar')
  // A dismissal re-renders through the hook's setter, so the chip leaves in
  // the same frame the builder opens, not on the next mount.
  const [dismissed, setDismissed] = usePersistedBool(AVATAR_HINT_DISMISSED_KEY, false)
  const open = useCallback(() => {
    if (!dismissed) setDismissed(true)
    onEdit()
  }, [dismissed, onEdit, setDismissed])
  // Glyph scaled to the face: ~45% of the edge, floored so a 22px face still
  // gets a legible pencil, capped so a 176px preview does not get a poster.
  const glyph = Math.min(20, Math.max(11, Math.round(size * 0.45)))
  const showHint = !!hint && !dismissed && !disabled

  return (
    <span className="inline-flex items-center gap-2 min-w-0">
      <button
        type="button"
        onClick={open}
        disabled={disabled}
        className="group/avatar relative inline-flex shrink-0 cursor-pointer rounded-md focus-ring disabled:cursor-not-allowed disabled:opacity-60"
        aria-label={label}
        title={label}
        data-testid={testId}
      >
        {children}
        {/* Hover/focus scrim. Hidden outright under (hover: none): a veil that
            only appears on tap would flash over the face as the builder opens. */}
        <span
          aria-hidden="true"
          data-testid="avatar-edit-scrim"
          className={`pointer-events-none absolute inset-0 flex items-center justify-center rounded-md bg-bg/60 text-text-strong opacity-0 transition-opacity duration-150 motion-reduce:transition-none group-hover/avatar:opacity-100 group-focus-visible/avatar:opacity-100 group-disabled/avatar:opacity-0 [@media(hover:none)]:hidden`}
        >
          <Pencil size={glyph} className="lucide-inline" />
        </span>
        {/* Touch fallback: no hover means no scrim, so the pencil lives on the
            corner permanently instead. Accent fill on a bg-colored ring, the
            presence dot's own idiom, so it reads as a badge and not as part
            of the drawn face. */}
        <span
          aria-hidden="true"
          data-testid="avatar-edit-badge"
          className="pointer-events-none absolute -right-1 -bottom-1 hidden [@media(hover:none)]:flex h-4 w-4 items-center justify-center rounded-full border-2 border-bg bg-accent text-accent-fg"
        >
          <Pencil size={9} className="lucide-inline" />
        </span>
      </button>
      {showHint && (
        <button
          type="button"
          onClick={open}
          data-testid="avatar-edit-hint"
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-accent-subtle bg-accent-subtle px-2 py-0.5 text-[11px] leading-4 text-accent cursor-pointer transition-colors hover:bg-accent/20 focus-ring"
        >
          <Pencil size={10} className="lucide-inline" />
          {t('components.avatarBuilder.hint_customize')}
        </button>
      )}
    </span>
  )
}
