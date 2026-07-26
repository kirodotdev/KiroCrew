// The identity badge shown next to an author across Issue Radar's detail panes
// (issues AND pull requests). Extracted so both panes render identity
// identically — a maintainer must not read as "Owner" on an issue and as
// nothing on a PR — and so the role vocabulary lives in exactly one place.

/** Human labels for GitHub's ``author_association`` vocabulary. */
const ASSOC_LABEL: Record<string, string> = {
  OWNER: 'Owner', MEMBER: 'Member', COLLABORATOR: 'Collaborator',
  CONTRIBUTOR: 'Contributor', FIRST_TIME_CONTRIBUTOR: 'First-time contributor',
  FIRST_TIMER: 'First-timer',
}

/** Human labels for a member's repo role (the collaborators roster) and, for the
 * read-only derived fallback, the author_association vocabulary. */
const ROLE_LABEL: Record<string, string> = {
  admin: 'Admin', maintain: 'Maintainer', write: 'Write', triage: 'Triage', read: 'Read',
  OWNER: 'Owner', MEMBER: 'Member', COLLABORATOR: 'Collaborator', member: 'Member',
}

/** Roles that are collaborators but not maintainers — muted rather than accent. */
const ROLE_MUTED = new Set(['read'])

/** Small role badge next to an author. Maintainers (owner/member/collaborator)
 * read as accent; first-timers as warn (a triage signal — they may need extra
 * guidance); other associations stay muted. NONE renders nothing. */
function AssociationBadge({ assoc }: { assoc?: string | null }) {
  if (!assoc || assoc === 'NONE') return null
  const label = ASSOC_LABEL[assoc]
  if (!label) return null
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
    const label = ROLE_LABEL[role] ?? role
    const cls = ROLE_MUTED.has(role) ? 'bg-bg-elevated text-muted' : 'bg-accent-subtle text-accent'
    return <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>{label}</span>
  }
  return <AssociationBadge assoc={assoc} />
}
