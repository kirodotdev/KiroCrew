/** Shared tab appearance for the workspace and terminal strips. */
export function panelTabClassName(active: boolean, { iconOnly = false, closable = true }: { iconOnly?: boolean; closable?: boolean } = {}) {
  return `group panel-tab relative flex items-center gap-1.5 h-8 rounded-full cursor-pointer shrink-0 max-w-[240px] select-none border transition-colors ${
    iconOnly ? 'w-8 justify-center p-0' : closable ? 'pl-3 pr-1.5' : 'px-3'
  } ${
    active ? 'bg-bg-elevated border-border text-text-strong shadow-sm' : 'bg-transparent border-transparent text-muted hover:text-text hover:bg-bg-hover'
  }`
}

export const PANEL_TAB_ICON_CLASS = 'panel-tab-icon flex items-center justify-center shrink-0'

export const PANEL_TAB_LIST_CLASS = 'flex items-center gap-2 min-w-0 overflow-x-auto scrollbar-none list-none m-0 p-0'
