import type { CSSProperties } from 'react'
import githubMarkUrl from '../assets/github-mark.svg'
import discordMarkUrl from '../assets/discord-mark.svg'
import coderMarkUrl from '../assets/coder-mark.svg'
import flyMarkUrl from '../assets/fly-mark.svg'
import boxdMarkUrl from '../assets/boxd-mark.svg'

/**
 * Monochrome brand marks (GitHub, Discord, the Kiro ghost) tinted via CSS
 * `mask` so they follow `currentColor` — matching the muted/hover treatment of
 * adjacent lucide icons. Same asset-file pattern as `SlackIcon` (Vite emits a
 * hashed URL under /assets); lucide-react ships no brand icons.
 *
 * Exported so every masked brand glyph shares ONE implementation of the
 * URL-quoting fix below — a second hand-rolled copy could silently miss a
 * future correction to it.
 */
export function BrandGlyph({ url, size, height, className = 'inline-block shrink-0', testId, style }: {
  url: string
  size: number
  /**
   * Box height, for marks whose art is not square (the onboarding mascot).
   * Defaults to `size`, so every existing square nav glyph is unchanged.
   */
  height?: number
  className?: string
  testId?: string
  /** Extra style (positioning). Merged last so callers can only add, not break the mask. */
  style?: CSSProperties
}) {
  return (
    <span
      aria-hidden="true"
      data-testid={testId}
      className={className}
      style={{
        width: size,
        height: height ?? size,
        backgroundColor: 'currentColor',
        // Quote the URL: in the production build these small SVGs are inlined
        // by Vite as `data:` URIs, whose commas/`#`/parens break an UNQUOTED
        // `url(...)` token (the mask silently fails and the span renders as a
        // solid currentColor box). Dev serves them as clean file paths, so the
        // bug only shows in the packaged app. Matches GithubLogo/GitlabLogo.
        WebkitMaskImage: `url("${url}")`,
        maskImage: `url("${url}")`,
        WebkitMaskRepeat: 'no-repeat',
        maskRepeat: 'no-repeat',
        WebkitMaskSize: 'contain',
        maskSize: 'contain',
        WebkitMaskPosition: 'center',
        maskPosition: 'center',
        ...style,
      }}
    />
  )
}

export function GithubIcon({ size = 16 }: { size?: number }) {
  return <BrandGlyph url={githubMarkUrl} size={size} />
}

export function DiscordIcon({ size = 16 }: { size?: number }) {
  return <BrandGlyph url={discordMarkUrl} size={size} />
}

/** Coder wordmark-style mark. Monochrome, so it is masked and follows the
 * theme's currentColor in light and dark. The art is about 2.17:1 wide. */
export function CoderIcon({ size = 16 }: { size?: number }) {
  return <BrandGlyph url={coderMarkUrl} size={Math.round(size * 2.17)} height={size} />
}

/** Fly.io balloon mark (Simple Icons artwork), for Sprites. Monochrome, so it is
 * masked and follows the theme's currentColor like the Coder mark. */
export function FlyIcon({ size = 16 }: { size?: number }) {
  return <BrandGlyph url={flyMarkUrl} size={size} />
}

/** boxd's notched-box mark, without its wordmark. Monochrome, so it is masked
 * and follows the theme's currentColor. */
export function BoxdIcon({ size = 16 }: { size?: number }) {
  return <BrandGlyph url={boxdMarkUrl} size={size} />
}
