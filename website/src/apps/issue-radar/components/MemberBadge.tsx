// The identity badge shown next to an author across Issue Radar's detail panes
// (issues AND pull requests). Extracted so both panes render identity
// identically — a maintainer must not read as "Owner" on an issue and as
// nothing on a PR — and so the role vocabulary lives in exactly one place.

import { i18nT } from '../../../i18n/t'

/**
 * Catalog KEYS for GitHub's ``author_association`` vocabulary.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in the components below, which run during render.
 *
 * The provider's raw enum values stay as the table's KEYS — only our own
 * humanised rendering of them is translated (`FIRST_TIME_CONTRIBUTOR` is the API
 * token; "First-time contributor" is copy we wrote).
 */
const ASSOC_LABEL_KEY: Record<string, string> = {
  OWNER: 'apps.issueRadar.components.memberBadge.owner',
  MEMBER: 'apps.issueRadar.components.memberBadge.member',
  COLLABORATOR: 'apps.issueRadar.components.memberBadge.collaborator',
  CONTRIBUTOR: 'apps.issueRadar.components.memberBadge.contributor',
  FIRST_TIME_CONTRIBUTOR: 'apps.issueRadar.components.memberBadge.first_time_contributor',
  FIRST_TIMER: 'apps.issueRadar.components.memberBadge.first_timer',
}

/** Catalog KEYS for a member's repo role (the collaborators roster) and, for the
 * read-only derived fallback, the author_association vocabulary. Same
 * keys-not-strings reasoning as above; the four association entries reuse the
 * keys declared there rather than duplicating the string. */
const ROLE_LABEL_KEY: Record<string, string> = {
  admin: 'apps.issueRadar.components.memberBadge.admin',
  maintain: 'apps.issueRadar.components.memberBadge.maintainer',
  write: 'apps.issueRadar.components.memberBadge.write',
  triage: 'apps.issueRadar.components.memberBadge.triage',
  read: 'apps.issueRadar.components.memberBadge.read',
  OWNER: 'apps.issueRadar.components.memberBadge.owner',
  MEMBER: 'apps.issueRadar.components.memberBadge.member',
  COLLABORATOR: 'apps.issueRadar.components.memberBadge.collaborator',
  member: 'apps.issueRadar.components.memberBadge.member',
}

/** Roles that are collaborators but not maintainers — muted rather than accent. */
const ROLE_MUTED = new Set(['read'])

/** Small role badge next to an author. Maintainers (owner/member/collaborator)
 * read as accent; first-timers as warn (a triage signal — they may need extra
 * guidance); other associations stay muted. NONE renders nothing. */
function AssociationBadge({ assoc }: { assoc?: string | null }) {
  if (!assoc || assoc === 'NONE') return null
  // `hasOwnProperty`, not a bare index: `assoc` is provider data, so a value like
  // `toString` would otherwise resolve to an inherited Object.prototype member
  // and hand a function to i18next.
  if (!Object.prototype.hasOwnProperty.call(ASSOC_LABEL_KEY, assoc)) return null
  const label = i18nT(ASSOC_LABEL_KEY[assoc])
  const isFirst = assoc === 'FIRST_TIME_CONTRIBUTOR' || assoc === 'FIRST_TIMER'
  const isMaint = assoc === 'OWNER' || assoc === 'MEMBER' || assoc === 'COLLABORATOR'
  const cls = isFirst ? 'bg-warn-subtle text-warn' : isMaint ? 'bg-accent-subtle text-accent' : 'bg-bg-elevated text-muted'
  return <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>{label}</span>
}

/** The badge shown next to an author. A repo-roster ROLE takes precedence
 * (Admin/Maintainer read as accent; read-only collaborators muted); when the
 * author isn't a member it falls back to their per-item author_association
 * (first-timer / contributor signals). */
export default function MemberBadge({ role, assoc }: { role?: string | null; assoc?: string | null }) {
  if (role) {
    // Same prototype-chain guard as above; an unknown role still shows verbatim.
    const label = Object.prototype.hasOwnProperty.call(ROLE_LABEL_KEY, role)
      ? i18nT(ROLE_LABEL_KEY[role])
      : role
    const cls = ROLE_MUTED.has(role) ? 'bg-bg-elevated text-muted' : 'bg-accent-subtle text-accent'
    return <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>{label}</span>
  }
  return <AssociationBadge assoc={assoc} />
}
