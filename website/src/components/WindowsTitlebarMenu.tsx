import { useCallback } from 'react'
import Clickable from './Clickable'

const WINDOWS_MENUS = [
  { id: 'file-menu', label: 'File' },
  { id: 'edit-menu', label: 'Edit' },
  { id: 'view-menu', label: 'View' },
  { id: 'connection-menu', label: 'Connection' },
  { id: 'window-menu', label: 'Window' },
  { id: 'help-menu', label: 'Help' },
] as const

type ElectronMenuAPI = {
  showAppMenu?: (id: string, anchor: { x: number; y: number }, mode: 'dark' | 'light') => void
}

/**
 * Windows replaces Electron's separate native menu row with these compact
 * triggers inside the dashboard's draggable 42px titlebar. The menu contents
 * stay native (and keep their accelerators/roles); only their anchors move.
 */
export default function WindowsTitlebarMenu() {
  const openMenu = useCallback((id: string, target: HTMLElement) => {
    const api = (window as Window & { electronAPI?: ElectronMenuAPI }).electronAPI
    if (!api?.showAppMenu) return
    const rect = target.getBoundingClientRect()
    const titlebarBottom = target.closest('header')?.getBoundingClientRect().bottom
    const mode = document.documentElement.dataset.mode === 'light' ? 'light' : 'dark'
    api.showAppMenu(id, { x: rect.left, y: titlebarBottom ?? rect.bottom }, mode)
  }, [])

  return (
    <nav className="windows-titlebar-menu flex h-full items-center gap-0.5" aria-label="Application menu">
      {WINDOWS_MENUS.map(menu => (
        <Clickable
          key={menu.id}
          className="windows-titlebar-menu-item inline-flex h-6 items-center justify-center rounded-md px-2 text-[12px] font-medium leading-none text-muted hover:bg-bg-hover hover:text-text focus-visible:bg-bg-hover focus-visible:text-text focus-visible:outline-none"
          aria-haspopup="menu"
          onClick={event => event && openMenu(menu.id, event.currentTarget as HTMLElement)}
        >
          {menu.label}
        </Clickable>
      ))}
    </nav>
  )
}
