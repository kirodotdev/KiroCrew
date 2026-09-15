/**
 * The Crew summary drawer as DATA (design step 6): an ordered list of
 * sections rendered by one loop, so reordering the drawer is a list edit, not
 * JSX surgery. The page builds the list; this module renders one entry and
 * hosts the two sections that are new with the design -- the member's own
 * Briefing and the Capabilities its definition brings along.
 */
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Link2, Sparkles } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { AGENTS_INSTALLED_QUERY_KEY } from './HireGalleryPage'

export type DrawerSection = {
  id: 'briefing' | 'capabilities' | 'template' | 'activity' | 'sessions' | 'patrol' | 'wake' | 'configuration'
  title: ReactNode
  body: ReactNode
  /** A control on the title row's right (the wake sources' Schedule jump). */
  action?: ReactNode
  /** Folded behind a disclosure; the page owns the open state. */
  collapsible?: boolean
}

/** The order the drawer reads in. Exported so a test pins it against the
 *  rendered document rather than against this file. `template` (the role
 *  update offer + detach) is present only for a member hired from a store
 *  card; it sits in the open part of the drawer, never behind the folded
 *  Configuration, because an offered update the owner cannot see is not
 *  an offer. */
export const DRAWER_SECTION_ORDER: readonly DrawerSection['id'][] = [
  'briefing', 'capabilities', 'template', 'activity', 'sessions', 'patrol', 'wake', 'configuration',
]

export function DrawerSectionView({ section, open, onToggle }: { section: DrawerSection; open: boolean; onToggle?: () => void }) {
  const titleCls = 'text-[11px] font-semibold tracking-wide text-muted mb-1.5 flex items-center gap-1.5'
  return (
    <section data-testid={`member-section-${section.id}`} data-section-open={open ? '1' : '0'}>
      {section.collapsible && onToggle ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className={`${titleCls} w-full bg-transparent border-none px-0 cursor-pointer text-left hover:text-text`}
          data-testid={`member-section-${section.id}-toggle`}
        >
          {open ? <ChevronDown size={12} className="lucide-inline shrink-0" aria-hidden /> : <ChevronRight size={12} className="lucide-inline shrink-0" aria-hidden />}
          <span className="flex-1">{section.title}</span>
          {section.action}
        </button>
      ) : (
        <div className={titleCls}>
          <span className="flex-1">{section.title}</span>
          {section.action}
        </div>
      )}
      {open && <div data-testid={`member-section-${section.id}-body`}>{section.body}</div>}
    </section>
  )
}

/** The member's own briefing (`members/<slug>/briefing.md`), read through the
 *  same pinned, fail-closed read the prompt builder uses. Read-only here: the
 *  file is the member's to maintain. */
export function MemberBriefing({ slug, member }: { slug: string; member: string }) {
  const { t } = useTranslation()
  const q = useQuery({
    queryKey: ['member-briefing', slug, member],
    queryFn: () => api.memberBriefing(slug, member),
    staleTime: 30_000,
  })
  if (q.isPending) {
    return (
      <div className="mb-4 space-y-1.5" data-testid="member-briefing-loading" aria-hidden>
        <div className="h-3 rounded bg-accent/40 animate-pulse" />
        <div className="h-3 w-3/4 rounded bg-accent/40 animate-pulse" />
      </div>
    )
  }
  if (q.isError) {
    return (
      <div className="mb-4">
        <ErrorNotice message={t('pages.membersPage.briefing_error')} variant="inline" askAgent testId="member-briefing-error" />
      </div>
    )
  }
  if (!q.data.supported) {
    return <div className="text-[11px] text-muted mb-4" data-testid="member-briefing-unsupported">{t('pages.membersPage.briefing_unsupported')}</div>
  }
  if (!q.data.text.trim()) {
    return <div className="text-[11px] text-muted mb-4" data-testid="member-briefing-empty">{t('pages.membersPage.briefing_none')}</div>
  }
  return (
    <pre className="mb-4 whitespace-pre-wrap break-words text-[11.5px] leading-snug font-body text-text max-h-[220px] overflow-y-auto border border-border rounded-md px-2.5 py-2 bg-bg-elevated" data-testid="member-briefing">
      {q.data.text}
    </pre>
  )
}

/** What the member's definition brings along -- its skills and MCP servers --
 *  read from the installed-agents listing for the file the member is bound
 *  to (its own copy after a hire). */
export function MemberCapabilities({ agent }: { agent: string }) {
  const { t } = useTranslation()
  const q = useQuery<{ name: string; skills?: string[]; mcp_servers?: string[] }[]>({
    queryKey: AGENTS_INSTALLED_QUERY_KEY,
    queryFn: () => api.agentsInstalled(),
    staleTime: 30_000,
  })
  if (q.isPending) {
    return (
      <div className="mb-4 space-y-1.5" data-testid="member-capabilities-loading" aria-hidden>
        <div className="h-3 rounded bg-accent/40 animate-pulse" />
      </div>
    )
  }
  if (q.isError) {
    return (
      <div className="mb-4">
        <ErrorNotice message={t('pages.membersPage.capabilities_error')} variant="inline" askAgent testId="member-capabilities-error" />
      </div>
    )
  }
  const row = Array.isArray(q.data) ? q.data.find((a) => a.name === agent) : undefined
  const skills = row?.skills ?? []
  const servers = row?.mcp_servers ?? []
  if (skills.length === 0 && servers.length === 0) {
    return <div className="text-[11px] text-muted mb-4" data-testid="member-capabilities-empty">{t('pages.membersPage.capabilities_none')}</div>
  }
  return (
    <ul className="list-none m-0 p-0 mb-4 grid grid-cols-2 gap-x-3 gap-y-1" data-testid="member-capabilities">
      {skills.map((s) => (
        <li key={`skill:${s}`} className="flex items-center gap-1.5 text-[11.5px] min-w-0">
          <Sparkles size={11} className="lucide-inline text-accent shrink-0" aria-hidden />
          <span className="truncate" title={s}>{s}</span>
        </li>
      ))}
      {servers.map((s) => (
        <li key={`mcp:${s}`} className="flex items-center gap-1.5 text-[11.5px] min-w-0">
          <Link2 size={11} className="lucide-inline text-accent shrink-0" aria-hidden />
          <span className="truncate" title={s}>{s}</span>
        </li>
      ))}
    </ul>
  )
}
