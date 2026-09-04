/**
 * Dropdown panel for HookSkillsSelect — renders inside a portal.
 * Separated for coverage isolation (createPortal doesn't render in happy-dom).
 */
import { useCallback, useLayoutEffect, useState } from 'react'
import { Brain, Minus } from 'lucide-react'
import { createPortal } from 'react-dom'
import { Input } from './ui'
import { i18nT } from '../i18n/t'

/**
 * Restore focus to the trigger after the menu closes, but only when it is still
 * connected to the document AND focusable. A trigger can be unmounted (the form
 * closed under the open menu) or hidden by the time Escape fires; calling
 * `.focus()` on a detached node silently moves focus to <body>, stranding the
 * keyboard user. `isConnected` catches the unmount; `offsetParent` (null when
 * the element or an ancestor is `display:none`) catches the hidden case that a
 * connection check alone misses.
 */
function restoreFocusIfSafe(el: HTMLElement | null): void {
  if (!el || !el.isConnected) return
  // offsetParent is null for display:none subtrees and for position:fixed
  // elements; a fixed trigger is unusual here (the trigger is an inline button),
  // so treat null as "not focusable" rather than guessing.
  if (el.offsetParent === null) return
  el.focus()
}

interface CatalogSkill {
  key: string
  name: string
  description?: string
}

interface Props {
  anchorRef: React.RefObject<HTMLElement>
  dropdownRef: React.Ref<HTMLDivElement>
  inputRef: React.Ref<HTMLInputElement>
  filter: string
  setFilter: (v: string) => void
  onClose: () => void
  selected: string[]
  filtered: CatalogSkill[]
  byKey: Map<string, CatalogSkill>
  onAdd: (key: string) => void
  onRemove: (key: string) => void
}

export default function HookSkillsDropdown({
  anchorRef, dropdownRef, inputRef, filter, setFilter,
  onClose, selected, filtered, byKey, onAdd, onRemove,
}: Props) {
  // The menu is a fixed-position portal, so it must be positioned from the
  // trigger's viewport rect. Reading getBoundingClientRect() once during render
  // freezes that position: any scroll or resize while the menu is open leaves it
  // detached from the trigger. Track the rect in state and recompute it on
  // scroll (capture phase, to catch scrolling in any ancestor container, not
  // just the window) and resize.
  const [rect, setRect] = useState<DOMRect | null>(
    () => anchorRef.current?.getBoundingClientRect() ?? null,
  )

  const recompute = useCallback(() => {
    setRect(anchorRef.current?.getBoundingClientRect() ?? null)
  }, [anchorRef])

  useLayoutEffect(() => {
    // Sync the rect on mount (the trigger may have moved between the initial
    // state read and the effect) and whenever the layout can shift under us.
    recompute()
    window.addEventListener('scroll', recompute, true)
    window.addEventListener('resize', recompute)
    return () => {
      window.removeEventListener('scroll', recompute, true)
      window.removeEventListener('resize', recompute)
    }
  }, [recompute, selected])

  if (!rect) return null
  return createPortal(
    // The panel's only listener is keyboard-only Escape-to-dismiss (plus the
    // focus restore the trigger needs), delegated to the container because focus
    // can be on the filter Input or on any row. Nothing activates the panel
    // itself — every affordance inside it is a <button> or an <Input> — and it
    // cannot take an interactive role: `menu` forbids the text input it holds.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- container-level Escape dismissal, which IS the keyboard path, not a mouse-only affordance
    <div
      ref={dropdownRef}
      className="fixed z-50 bg-bg-elevated border border-border rounded-lg shadow-xl p-1 w-72 max-h-60 overflow-y-auto"
      style={{
        top: rect.bottom + 4,
        left: rect.left,
      }}
      onKeyDown={e => { if (e.key === 'Escape') { onClose(); restoreFocusIfSafe(anchorRef.current) } }}
    >
      <Input
        ref={inputRef}
        placeholder={i18nT('components.skillsMultiSelect.filter_skills')}
        value={filter}
        onChange={e => setFilter(e.target.value)}
        className="mb-1 text-[12px]"
        autoFocus
      />
      {selected.length > 0 && (
        <>
          <p className="text-[11px] text-muted px-2 pt-1 pb-0.5 font-medium uppercase tracking-wide">{i18nT('components.skillsMultiSelect.selected')}</p>
          {selected.map(key => {
            const skill = byKey.get(key)
            return (
              <button
                key={key}
                className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-danger-subtle transition-colors flex items-center gap-2"
                onClick={() => onRemove(key)}
                aria-label={i18nT('components.skillsMultiSelect.remove_skill', { name: skill?.name || key })}
              >
                <Minus className="lucide-inline shrink-0 text-danger" />
                <span className="flex flex-col min-w-0">
                  <span className="font-medium truncate">{skill?.name || key.split('/').pop()}</span>
                  <span className="text-muted text-[11px] font-mono truncate">{key}</span>
                </span>
              </button>
            )
          })}
          {filtered.length > 0 && <hr className="my-1 border-border" />}
        </>
      )}
      {filtered.length === 0 && selected.length === 0 && (
        <p className="text-[12px] text-muted px-2 py-1">{i18nT('components.skillsMultiSelect.no_matching_skills')}</p>
      )}
      {filtered.map(s => (
        <button
          key={s.key}
          className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-accent-subtle transition-colors flex items-center gap-2"
          onClick={() => onAdd(s.key)}
        >
          <Brain className="lucide-inline shrink-0 text-accent" />
          <span className="flex flex-col min-w-0">
            <span className="font-medium truncate">{s.name}</span>
            <span className="text-muted text-[11px] font-mono truncate">{s.key}</span>
          </span>
        </button>
      ))}
    </div>,
    document.body,
  )
}
