import { useMemo, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { folderPaneTint } from '../components/folderColorPaint'
import { resolveFolderTintColor } from '../utils/folderTint'
import { useOptionalTheme } from './useTheme'
import type { ChatFolder } from '../types'

export interface FolderTint {
  /** The folder's palette color (`#rrggbb`) the tint derives from. */
  color: string
  /** Inline style for the chat pane root. When the theme's `--bg` can be read,
   *  the pane redefines `--bg` for its subtree, so the header, the composer's
   *  fade and every `bg-bg` surface inside it sit on the same tinted canvas
   *  instead of painting the plain theme color over it. */
  style: CSSProperties
}

/** The theme's resolved `--bg`, or '' when it cannot be read. */
function readThemeBg(): string {
  if (typeof document === 'undefined') return ''
  return getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
}

/** The tint a chat pane takes from the folder its session is filed in, or null
 *  for no tint. Shares the sidebar's `['chat-folders']` cache, so it costs no
 *  extra request; `select` keeps the pane from re-rendering on folder changes
 *  that leave its color alone. */
export function useFolderTint(folderId: string | null | undefined): FolderTint | null {
  const { data: color } = useQuery<ChatFolder[], Error, string | null>({
    queryKey: ['chat-folders'],
    // Same guard as the page's own read of this key: a partially-mocked `api`
    // must not take the chat down over a decoration.
    queryFn: () => (typeof api?.chatFolders === 'function' ? api.chatFolders() : Promise.resolve([])),
    select: folders => (Array.isArray(folders) ? resolveFolderTintColor(folders, folderId) : null),
    // The sidebar keeps this cache current (it invalidates on every folder
    // write), so a pane mounting inside the window reads it without a request.
    // Same window as the command launcher's reads of this key.
    staleTime: 30_000,
    enabled: !!folderId,
  })
  // Keyed on themeVersion alone: it is bumped by the effect that writes the
  // theme attribute, so the read sees the palette that mode actually renders.
  const themeVersion = useOptionalTheme()?.themeVersion ?? 0
  const themeBg = useMemo(readThemeBg, [themeVersion])
  return useMemo(() => {
    if (!color) return null
    const style = themeBg
      ? ({ '--bg': folderPaneTint(color, themeBg), background: 'var(--bg)' } as CSSProperties)
      : { background: folderPaneTint(color) }
    return { color, style }
  }, [color, themeBg])
}
