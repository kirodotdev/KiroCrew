/** The sidebar's Projects section: the top-level Projects (folders with
 *  `kind: 'project'`, made by New > Project), under their own heading and above
 *  the ordinary folders, which render below it exactly as they do without one.
 *  Drawn only while a Project is visible, so a sidebar with none draws nothing
 *  new. */
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import type { ReactNode } from 'react'
import { i18nT } from '../../i18n/t'

/** The same small heading the sidebar's Local / crew groups use. */
const HEADER_CLS = 'm-0 w-full flex items-center gap-1.5 pl-2 pr-3 pt-3 pb-1 text-[11px] font-semibold text-muted select-none'

export function ProjectsSection({ ids, children }: { ids: readonly string[]; children: ReactNode }) {
  if (ids.length === 0) return null
  return (
    <section aria-labelledby="sidebar-projects-heading" data-testid="sidebar-projects-section" className="flex flex-col">
      <h2 id="sidebar-projects-heading" className={HEADER_CLS} data-testid="sidebar-projects-heading">
        <span className="min-w-0 truncate">{i18nT('pages.chatSidebar.projects_section')}</span>
      </h2>
      {/* Its own reorder ring: a Project moves among Projects only, and the
       *  ordinary folders' ring below never offers it a slot. */}
      <SortableContext items={[...ids]} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
      <div aria-hidden="true" className="mx-3 mt-1 mb-1 border-t border-border/60" data-testid="sidebar-projects-end" />
    </section>
  )
}
