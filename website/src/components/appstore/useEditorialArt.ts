/**
 * Theme selection for CURATOR-authored spotlight artwork.
 *
 * Deliberately not folded into `useHeroArt`. That hook resolves art an APP
 * declares in its own manifest, through the blob proxy, with a repo-relative
 * fallback chain ending in the first screenshot. This one resolves art the
 * CATALOG hosts: the URLs arrive already absolute and already screened by the
 * server (`_resolve_ref` drops anything that is not a plain path, which is what
 * keeps `javascript:` and `data:` — neither of which has a slash after its colon
 * — out of an `<img>` src). Two sources, two trust levels, two hooks.
 *
 * Editorial artwork WINS over an app's own hero image where both exist: the
 * spotlight is an editorial placement, so the curator's picture is the point.
 */
import { useEffect, useState } from 'react'
import { useTheme } from '../../hooks/useTheme'

export type EditorialArtwork = {
  url: string
  urlDark?: string
  alt?: string
}

export function useEditorialArt(artwork?: EditorialArtwork | null): {
  src: string
  alt: string
  onError: () => void
} {
  const { theme } = useTheme()
  const dark = theme === 'dark'
  // Falls back in BOTH directions, like `AppIcon`: the dark variant is optional,
  // and a section that only resolved one of them still renders that one.
  const chosen = artwork
    ? (dark ? artwork.urlDark || artwork.url : artwork.url || artwork.urlDark) || ''
    : ''
  const [failed, setFailed] = useState('')
  // Reset the failure latch when the chosen URL changes (a theme flip, or a
  // re-fetch that published new artwork) so the new URL gets a fresh attempt.
  useEffect(() => { setFailed('') }, [chosen])
  const src = chosen && chosen !== failed ? chosen : ''
  return {
    src,
    // Empty alt is CORRECT for decorative art: the section's own heading and
    // blurb already name the content, so announcing the image would repeat it.
    alt: (src && artwork?.alt) || '',
    onError: () => setFailed(chosen),
  }
}
