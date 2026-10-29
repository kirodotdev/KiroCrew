/**
 * The current row still fires `onSelect` so the owner can re-reveal its chip.
 * The menu sets its own `scrollTop` because `scrollIntoView` could move the page.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { IconButton } from './ui'

export interface AllTabsRow {
  id: string
  glyph?: ReactNode
  /** First line. A node, so a live title component (a terminal's) can be passed. */
  title: ReactNode
  secondary?: string
  badge?: ReactNode
  /** Native tooltip with the full text, for a title or path the row truncates. Plain text only. */
  tooltip?: string
}

export interface AllTabsGroup {
  id: string
  rows: AllTabsRow[]
}

export interface AllTabsMenuProps {
  /** Empty groups are dropped, and separators sit only between surviving groups. */
  groups: AllTabsGroup[]
  currentId: string | null
  /** Called for every selection, including the current row. */
  onSelect: (id: string) => void
  label: string
}

export const ALL_TABS_SECONDARY_CLASS = 'truncate text-[11px] leading-4 text-muted'
export const ALL_TABS_MARK_SLOT_CLASS = 'flex w-[13px] shrink-0 items-center justify-center'
export const ALL_TABS_CURRENT_TITLE_CLASS = 'text-accent'

/** Sets only `content.scrollTop`, by the minimum needed to show `row`. `scrollIntoView` could move ancestor scrollers. */
function revealRowInContent(content: HTMLElement, row: HTMLElement): void {
  const contentRect = content.getBoundingClientRect()
  const rowRect = row.getBoundingClientRect()
  // Rects are scaled while the menu's open animation runs, scrollTop is not, so convert.
  const scale = content.offsetHeight > 0 && contentRect.height > 0 ? contentRect.height / content.offsetHeight : 1
  const visibleTop = contentRect.top + content.clientTop * scale
  const visibleBottom = content.clientHeight > 0
    ? visibleTop + content.clientHeight * scale
    : contentRect.bottom
  if (rowRect.top < visibleTop) {
    content.scrollTop = content.scrollTop - (visibleTop - rowRect.top) / scale
  } else if (rowRect.bottom > visibleBottom) {
    // A row taller than the viewport aligns its top, so its title stays visible.
    const down = Math.min(rowRect.bottom - visibleBottom, rowRect.top - visibleTop) / scale
    content.scrollTop = content.scrollTop + down
  }
}

export function AllTabsMenu({ groups, currentId, onSelect, label }: AllTabsMenuProps): JSX.Element {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  // In state, not a ref: the portalled content mounts after `open` flips, and its arrival must re-run the scroll effect.
  const [contentEl, setContentEl] = useState<HTMLDivElement | null>(null)
  const rowEls = useRef(new Map<string, HTMLElement>())
  const focusedIdRef = useRef<string | null>(null)

  const surviving = groups.filter(g => g.rows.length > 0)

  const handleOpenChange = useCallback((next: boolean) => {
    if (!next) focusedIdRef.current = null
    setOpen(next)
  }, [])

  // The focused row's tab is gone: close and return focus to the trigger.
  useEffect(() => {
    const focusedId = focusedIdRef.current
    if (!open || focusedId == null) return
    const present = groups.some(g => g.rows.some(r => r.id === focusedId))
    if (present) return
    focusedIdRef.current = null
    setOpen(false)
    triggerRef.current?.focus()
    // Reacts to rows disappearing, so it is keyed on `groups` only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups])

  // Radix applies the menu's max-height over several layout passes after it opens, so the
  // content only becomes scrollable once its height settles. Keep the current row in view
  // across those passes, and stop at the first wheel, touch, key or press so a later
  // resize never undoes the person's own scrolling.
  useLayoutEffect(() => {
    if (!open || !contentEl || currentId == null) return
    const reveal = () => {
      const row = rowEls.current.get(currentId)
      if (row) revealRowInContent(contentEl, row)
    }
    reveal()
    const observer = new ResizeObserver(reveal)
    observer.observe(contentEl)
    const stop = () => observer.disconnect()
    const takeovers = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
    for (const type of takeovers) contentEl.addEventListener(type, stop, { passive: true })
    return () => {
      observer.disconnect()
      for (const type of takeovers) contentEl.removeEventListener(type, stop)
    }
  }, [open, currentId, contentEl])

  return (
    <DropdownMenu open={open} onOpenChange={handleOpenChange}>
      <DropdownMenuTrigger asChild>
        <IconButton
          ref={triggerRef}
          // mc-touch-hit-y gives a 44px coarse-pointer height without overlapping the adjacent +.
          className="mc-touch-hit-y flex items-center justify-center w-7 h-7 shrink-0 self-center rounded-md p-0 data-[state=open]:bg-bg-hover data-[state=open]:text-text transition-colors"
          aria-label={label}
          title={label}
          data-testid="all-tabs-trigger"
        >
          <ChevronDown size={15} aria-hidden="true" />
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={6} className="min-w-[240px] max-w-[min(360px,calc(100vw-16px))]" ref={setContentEl}>
        <DropdownMenuRadioGroup value={currentId ?? ''}>
          {surviving.map((group, i) => (
            <GroupRows
              key={group.id}
              group={group}
              separatorBefore={i > 0}
              currentId={currentId}
              onSelect={onSelect}
              rowEls={rowEls.current}
              focusedIdRef={focusedIdRef}
              contentEl={contentEl}
            />
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function GroupRows({ group, separatorBefore, currentId, onSelect, rowEls, focusedIdRef, contentEl }: {
  group: AllTabsGroup
  separatorBefore: boolean
  currentId: string | null
  onSelect: (id: string) => void
  rowEls: Map<string, HTMLElement>
  focusedIdRef: { current: string | null }
  contentEl: HTMLDivElement | null
}): JSX.Element {
  return (
    <>
      {separatorBefore && <DropdownMenuSeparator />}
      {group.rows.map(row => {
        const current = row.id === currentId
        return (
          <DropdownMenuRadioItem
            key={row.id}
            value={row.id}
            onSelect={() => onSelect(row.id)}
            data-row-id={row.id}
            title={row.tooltip}
            ref={(el: HTMLDivElement | null) => {
              if (el) rowEls.set(row.id, el)
              else rowEls.delete(row.id)
            }}
            onFocus={() => { focusedIdRef.current = row.id }}
            onBlur={(e) => {
              // A removed row's blur has no target, and the focus rule above depends on that id surviving.
              const next = e.relatedTarget
              if (next instanceof Node && (next === contentEl || !contentEl?.contains(next))) {
                if (focusedIdRef.current === row.id) focusedIdRef.current = null
              }
            }}
          >
            {row.glyph != null && <span className="text-muted shrink-0">{row.glyph}</span>}
            <span className="min-w-0 flex-1 flex flex-col">
              <span className={'truncate' + (current ? ' ' + ALL_TABS_CURRENT_TITLE_CLASS : '')}>{row.title}</span>
              {row.secondary && <span className={ALL_TABS_SECONDARY_CLASS}>{row.secondary}</span>}
            </span>
            {row.badge}
            <span className={ALL_TABS_MARK_SLOT_CLASS}>
              {current ? <Check size={13} className="text-accent" aria-hidden="true" /> : null}
            </span>
          </DropdownMenuRadioItem>
        )
      })}
    </>
  )
}
