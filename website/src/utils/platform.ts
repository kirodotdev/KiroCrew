/** Platform detection utilities for keyboard shortcut labels */
export const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** Format a shortcut string like "Shift+Enter" for the current platform */
export const platformShortcut = (shortcut: string): string =>
  isMac
    ? shortcut
        .replace(/Cmd\+/g, '⌘')
        .replace(/Ctrl\+/g, '⌃')
        .replace(/Shift\+/g, '⇧')
        .replace(/Alt\+/g, '⌥')
        .replace(/\bEnter\b/g, '↵')
    : shortcut.replace(/Cmd\+/g, 'Ctrl+')
