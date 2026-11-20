/**
 * Who may Edit & Resend a user row or rewind to it: the dashboard's copy of the
 * gateway's `kiro_crew.shared_crew.edit_rule`, so the pencil, the double-click
 * and Cmd/Ctrl+Up are offered only where the gateway accepts the edit. Both
 * sides are pinned by one table, `test/fixtures/shared_crew_edit_rule.json`.
 *
 * Display only: the gateway decides, and refuses with `not_message_author`.
 * Until the mode route answers (or when it cannot be read) every row stays
 * editable, as before this rule, and the gateway's refusal is what the user sees.
 */
import type { CrewModeResponse } from './sharedCrewMode'

/** The viewer's side of the rule, read from the mode route. */
export interface EditRuleViewer {
  /** Every principal id the viewer writes as (the mode route's `viewer_ids`). */
  ids: ReadonlySet<string>
  isOwner: boolean
  relayTeammate: boolean
  /** The Shared Crew preview is on. */
  preview: boolean
}

const PRINCIPAL_ID = /^p_[a-z0-9_]{1,64}$/
const RELAY_PROVIDER = 'kiro'

/** The viewer from a mode body, or `null` when the gateway sent no `edit_rule`. */
export function editRuleViewer(mode: CrewModeResponse | null | undefined): EditRuleViewer | null {
  const rule = mode?.edit_rule
  if (!mode || !rule) return null
  return { ids: new Set(mode.viewer_ids ?? []), isOwner: rule.is_owner === true, relayTeammate: rule.relay_teammate === true, preview: rule.preview === true }
}

/** The stored author as the gateway reads it: a stamp whose id does not parse is Unknown. */
function storedAuthor(meta: Record<string, unknown> | undefined): { id: string; kind: string; provider: string } {
  const raw = meta?.author
  const a = raw && typeof raw === 'object' ? raw as Record<string, unknown> : null
  if (!a || typeof a.id !== 'string' || !PRINCIPAL_ID.test(a.id)) return { id: '', kind: 'unknown', provider: '' }
  const kind = a.kind === 'human' || a.kind === 'machine' || a.kind === 'system' ? a.kind : 'unknown'
  return { id: a.id, kind, provider: typeof a.provider === 'string' ? a.provider : '' }
}

/** Whether *viewer* may edit or rewind to the user row carrying *meta*. */
export function mayEdit(viewer: EditRuleViewer | null, meta: Record<string, unknown> | undefined): boolean {
  if (!viewer) return true
  const author = storedAuthor(meta)
  const person = author.kind === 'human' || author.kind === 'machine'
  const authorRelay = person && author.provider === RELAY_PROVIDER
  if (!viewer.preview && !viewer.relayTeammate && !authorRelay) return true
  if (person) return viewer.ids.has(author.id)
  return viewer.isOwner || (author.kind === 'system' && viewer.ids.has(author.id))
}
