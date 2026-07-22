import githubMarkUrl from '../assets/github-mark.svg'
import discordMarkUrl from '../assets/discord-mark.svg'

/**
 * Monochrome brand marks (GitHub, Discord) tinted via CSS `mask` so they
 * follow `currentColor` — matching the muted/hover treatment of adjacent
 * lucide icons. Same asset-file pattern as `SlackIcon` (Vite emits a hashed
 * URL under /assets); lucide-react ships no brand icons.
 */
function BrandGlyph({ url, size }: { url: string; size: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block shrink-0"
      style={{
        width: size,
        height: size,
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
