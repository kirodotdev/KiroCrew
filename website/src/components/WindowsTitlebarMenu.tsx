import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { Check, Menu } from 'lucide-react'
import Clickable from './Clickable'
import { i18nT } from '../i18n/t'

const WINDOWS_MENUS = [
  { id: 'file-menu', label: 'File' },
  { id: 'edit-menu', label: 'Edit' },
  { id: 'view-menu', label: 'View' },
  { id: 'connection-menu', label: 'Connection' },
  { id: 'window-menu', label: 'Window' },
  { id: 'help-menu', label: 'Help' },
] as const

const WINDOWS_MENU_POPUP_WIDTH = 224
const WINDOWS_MENU_VIEWPORT_GUTTER = 8

const formatWindowsAccelerator = (accelerator: string) => accelerator
  .replaceAll('CommandOrControl', 'Ctrl')
  .replaceAll('CmdOrCtrl', 'Ctrl')

type ElectronMenuAPI = {
  getAppMenuItems?: (id: string) => Promise<AppMenuItem[]>
  executeAppMenuItem?: (id: string, index: number) => void
}

type AppMenuItem =
  | { type: 'separator'; index: number }
  | {
      type: 'normal' | 'checkbox' | 'radio'
      index: number
      label: string
      accelerator: string
      enabled: boolean
      checked: boolean
    }

type WindowsTitlebarMenuProps = {
  onExpandedChange?: (expanded: boolean) => void
}

/**
 * Zed-style Windows application menu. It rests as a compact hamburger, expands
 * the top-level labels while a submenu is open, and switches submenus on hover.
 * The submenu is drawn here rather than by Menu.popup() because a native popup
 * captures window input on Windows, which kills hover switching; only the item
 * model and the command dispatch cross the IPC bridge, so Electron's roles,
 * enabled/checked state and accelerators stay authoritative. Escape, an outside
 * pointerdown, window blur, or picking a command ends the menu session.
 */
export default function WindowsTitlebarMenu({ onExpandedChange }: WindowsTitlebarMenuProps) {
  const [expanded, setExpanded] = useState(false)
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null)
  const [menuItems, setMenuItems] = useState<AppMenuItem[]>([])
  const [popupPosition, setPopupPosition] = useState({ left: 0, top: 0 })
  const menuItemRefs = useRef(new Map<string, HTMLDivElement>())
  const popupRef = useRef<HTMLDivElement>(null)
  const requestIdRef = useRef(0)

  const setMenuExpanded = useCallback((next: boolean) => {
    setExpanded(next)
    onExpandedChange?.(next)
  }, [onExpandedChange])

  const collapseMenu = useCallback(() => {
    requestIdRef.current += 1
    setActiveMenuId(null)
    setMenuItems([])
    setMenuExpanded(false)
  }, [setMenuExpanded])

  useEffect(() => {
    if (!expanded) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target
      if (!(target instanceof Node)) return
      if (popupRef.current?.contains(target)) return
      if (target instanceof Element && target.closest('.windows-titlebar-menu')) return
      collapseMenu()
    }
    window.addEventListener('pointerdown', onPointerDown, true)
    window.addEventListener('blur', collapseMenu)
    return () => {
      window.removeEventListener('pointerdown', onPointerDown, true)
      window.removeEventListener('blur', collapseMenu)
    }
  }, [collapseMenu, expanded])

  const openMenu = useCallback(async (id: string, target: HTMLElement) => {
    const api = (window as Window & { electronAPI?: ElectronMenuAPI }).electronAPI
    if (!api?.getAppMenuItems) return
    const rect = target.getBoundingClientRect()
    const titlebarBottom = target.closest('header')?.getBoundingClientRect().bottom
    const requestId = ++requestIdRef.current
    setActiveMenuId(id)
    setMenuItems([])
    setMenuExpanded(true)
    setPopupPosition({
      left: Math.max(
        WINDOWS_MENU_VIEWPORT_GUTTER,
        Math.min(rect.left, window.innerWidth - WINDOWS_MENU_POPUP_WIDTH - WINDOWS_MENU_VIEWPORT_GUTTER),
      ),
      top: titlebarBottom ?? rect.bottom,
    })
    try {
      const items = await api.getAppMenuItems(id)
      if (requestIdRef.current === requestId) setMenuItems(items)
    } catch {
      if (requestIdRef.current === requestId) collapseMenu()
    }
  }, [collapseMenu, setMenuExpanded])

  const handleMenuClick = useCallback((id: string, target: HTMLElement) => {
    if (activeMenuId === id) collapseMenu()
    else openMenu(id, target)
  }, [activeMenuId, collapseMenu, openMenu])

  const executeItem = useCallback((item: Exclude<AppMenuItem, { type: 'separator' }>) => {
    const api = (window as Window & { electronAPI?: ElectronMenuAPI }).electronAPI
    if (activeMenuId && item.enabled) api?.executeAppMenuItem?.(activeMenuId, item.index)
    collapseMenu()
  }, [activeMenuId, collapseMenu])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (!expanded) return
    if (event.key === 'Escape') {
      event.preventDefault()
      collapseMenu()
      return
    }
    if (event.key === 'ArrowDown' && !popupRef.current?.contains(event.target as Node)) {
      event.preventDefault()
      popupRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
      return
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const activeIndex = WINDOWS_MENUS.findIndex(menu => menu.id === activeMenuId)
    const direction = event.key === 'ArrowRight' ? 1 : -1
    const nextIndex = (Math.max(0, activeIndex) + direction + WINDOWS_MENUS.length) % WINDOWS_MENUS.length
    const nextMenu = WINDOWS_MENUS[nextIndex]
    const target = menuItemRefs.current.get(nextMenu.id)
    if (!target) return
    target.focus()
    openMenu(nextMenu.id, target)
  }, [activeMenuId, collapseMenu, expanded, openMenu])

  return (
    <nav
      className="windows-titlebar-menu flex h-full shrink-0 items-center gap-0.5"
      aria-label={i18nT('app.open_menu')}
      onKeyDown={handleKeyDown}
    >
      {!expanded && (
        <Clickable
          className="windows-titlebar-menu-item inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-bg-hover hover:text-text focus-visible:bg-bg-hover focus-visible:text-text focus-visible:outline-none"
          aria-haspopup="menu"
          aria-expanded="false"
          aria-label={i18nT('app.open_menu')}
          onClick={event => {
            const target = event?.currentTarget as HTMLElement | undefined
            if (target) openMenu(WINDOWS_MENUS[0].id, target)
          }}
        >
          <Menu className="lucide-inline" size={16} aria-hidden="true" />
        </Clickable>
      )}
      {expanded && WINDOWS_MENUS.map(menu => (
        <Clickable
          key={menu.id}
          ref={node => {
            if (node) menuItemRefs.current.set(menu.id, node)
            else menuItemRefs.current.delete(menu.id)
          }}
          className={`windows-titlebar-menu-item inline-flex h-7 items-center justify-center rounded-md px-2 text-[12px] font-medium leading-none transition-colors focus-visible:outline-none ${activeMenuId === menu.id ? 'bg-bg-hover text-text' : 'text-muted hover:bg-bg-hover hover:text-text focus-visible:bg-bg-hover focus-visible:text-text'}`}
          aria-haspopup="menu"
          aria-expanded={activeMenuId === menu.id}
          onMouseEnter={event => {
            if (activeMenuId !== menu.id) openMenu(menu.id, event.currentTarget)
          }}
          onClick={event => event && handleMenuClick(menu.id, event.currentTarget as HTMLElement)}
        >
          {menu.label}
        </Clickable>
      ))}
      {expanded && activeMenuId && createPortal(
        <div
          ref={popupRef}
          role="menu"
          tabIndex={-1}
          className="fixed z-[9999] max-h-[calc(100vh-50px)] min-w-56 overflow-y-auto rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg"
          style={{ left: popupPosition.left, top: popupPosition.top }}
          onKeyDown={event => {
            if (event.key === 'Escape') {
              event.preventDefault()
              collapseMenu()
              return
            }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
            event.preventDefault()
            event.stopPropagation()
            const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
            if (buttons.length === 0) return
            const currentIndex = buttons.indexOf(document.activeElement as HTMLButtonElement)
            const nextIndex = event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? buttons.length - 1
                : (Math.max(0, currentIndex) + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
            buttons[nextIndex]?.focus()
          }}
        >
          {menuItems.map(item => item.type === 'separator' ? (
            <div key={item.index} role="separator" className="mx-1 my-1 h-px bg-border" />
          ) : (
            <button
              key={item.index}
              type="button"
              role={item.type === 'normal' ? 'menuitem' : 'menuitemcheckbox'}
              aria-checked={item.type === 'normal' ? undefined : item.checked}
              disabled={!item.enabled}
              className="flex w-full cursor-pointer select-none items-center gap-2 rounded-md border-none bg-transparent px-3 py-1.5 text-left text-[13px] text-text outline-none transition-colors hover:bg-bg-hover focus:bg-bg-hover disabled:pointer-events-none disabled:opacity-50"
              onClick={() => executeItem(item)}
            >
              <span className="flex size-3 shrink-0 items-center justify-center">
                {item.type !== 'normal' && item.checked && (
                  <Check className="lucide-inline" size={12} aria-hidden="true" />
                )}
              </span>
              <span className="flex-1 whitespace-nowrap">{item.label}</span>
              {item.accelerator && (
                <span className="ml-6 whitespace-nowrap text-[11px] text-muted">
                  {formatWindowsAccelerator(item.accelerator)}
                </span>
              )}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </nav>
  )
}
