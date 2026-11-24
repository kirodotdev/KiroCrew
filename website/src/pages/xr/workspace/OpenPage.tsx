/**
 * The plus in the flat preview's bar: the pages a person can open, as a list. A tap on one opens that page on a
 * new panel. With the gateway's panels open the list is the dashboard's own navigation; with them guarded it is
 * the short list that the palm holds in a session (`Palette.tsx`). Both read `pageMenu.tsx`. The list is as tall
 * as the window has room for, and scrolls by a drag and by the wheel when it is longer. A drag over it opens no row,
 * and a row of a list that changed in the last second takes no press.
 */
import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { api } from '../../../api/client'
import { Btn } from '../../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { i18nT } from '../../../i18n/t'
import { menuLabel, pagesToOpen } from './pageMenu'
import { usePanelWay } from './panelWay'

/** A row of a list that came or moved this recently takes no press (ms): the press was aimed at what stood there before. */
const LIST_CALM_MS = 1000
/** A press that travelled this far over the list is a drag that scrolls it, and opens no row (CSS px). */
const LIST_DRAG_PX = 8

export function OpenPage({ onOpen }: { onOpen: (path: string) => void }) {
  const way = usePanelWay()
  // The read that the dashboard's rail judges a page's own setting by.
  const { data: config } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const pages = pagesToOpen(way, config)
  // The list is read from the dashboard's state, which can change while it stands (a setting that loads late).
  const rows = useMemo(() => pages.map(page => page.path).join('|'), [pages])
  const changedAt = useRef(0)
  useEffect(() => { changedAt.current = performance.now() }, [rows])
  const press = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const take = (event: Event) => {
    const held = press.current
    if ((held && held.moved) || performance.now() - changedAt.current < LIST_CALM_MS) event.preventDefault()
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Btn aria-label={i18nT('xr.open_page')} title={i18nT('xr.open_page')} data-testid="xr-open-page">
          <Plus size={15} />
        </Btn>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="min-w-[200px] max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto overscroll-contain"
        data-testid="xr-open-page-list"
        onPointerDown={e => { press.current = { x: e.clientX, y: e.clientY, moved: false } }}
        onPointerMove={e => { const p = press.current; if (p && Math.hypot(e.clientX - p.x, e.clientY - p.y) > LIST_DRAG_PX) p.moved = true }}
      >
        {pages.map(page => (
          <DropdownMenuItem key={page.path} onSelect={e => { take(e); if (!e.defaultPrevented) onOpen(page.path) }} data-testid={`xr-open-page-${page.path.slice(1)}`} data-page={page.path}>
            <span className="inline-flex w-5 shrink-0 justify-center text-muted">{page.icon}</span>
            <span>{menuLabel(page)}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
