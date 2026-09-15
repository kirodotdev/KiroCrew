/**
 * Isolated capture entry for unfurled link previews — favicon legibility.
 *
 * WHY ISOLATED: a chip only exists inside a rendered assistant turn, and booting
 * the full SPA to get one needs the app shell, a live websocket and a seeded
 * session; a half-stubbed shell renders its error boundary, which is worse
 * evidence than none.
 *
 * What MUST be faithful here is the DECISION, because the whole change is "the
 * icon is chosen and plated from measured pixels". So nothing about the
 * component is mocked: `fetch` is stubbed at the same `/api/link-meta` seam the
 * real hook uses, and the real `MarkdownRenderer` -> `LinkChip` / `LinkCard` ->
 * `iconContrast` path then samples real favicon bytes against the real
 * stylesheet's theme tokens, exactly as it does in production.
 *
 * Scene + theme come from the query string: ?scene=chips&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// MarkdownRenderer's overflow paths reach for useNavigate, so a router has to be
// in scope even though nothing here navigates.
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it, every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'chips'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/**
 * Real favicon bytes, base64'd so the capture is deterministic and offline.
 *
 * `GITHUB_LIGHT` is what `https://github.com/` actually declares in its markup
 * (`favicons/favicon.png`) — a near-black glyph drawn for a white browser tab,
 * and the icon from the original report. `GITHUB_DARK` is the white counterpart
 * the site swaps in from JavaScript (`favicons/favicon-dark.png`), used here as
 * the `icon_dark` a site that DECLARES its variant in markup would send.
 */
const GITHUB_LIGHT =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAANTSURBVHgBvVeNTdtAFH7vbCilUptuYFQihUhNzQQJEzSZoGSChgkIEwATNJ0AmICM4ISKWCKoHiGtVDUCn1/v2cRx4nNIcNRPgpzvnd/fvT8jLAnLsgrGVqEOQlaR0AYECwgKIRFhpNYeITkQYFeOf195njdahi8+K7hUsgzcaCPi51jgElDKdGTweOK5rgcvUYAtNl+/PVbLFuSBoDP/j3Hiec5oaQUiqzevFdGCNYAAPEkPBzpviPmNYrlsr1M4g3kxz2KxbGtoU6zb8nnoPBF7IIzyOeH8gvp/Gf2uCJUZRNRRK2e6FXnCsuxCSgEOuJTlBO3h4KZxP+jvKGbNWBFOu5AxdeO/aO9JMBwOb/vv793bJlBwNKsXWOYbeZx4jlxv4ubPlBG+2L+7c2ILLFtpPh4XslLLKtkWjGGUjHjeMzFI8fbpYYf5mPzAea5j+GjATOp4Tsg4s8B4ruPBkjDEBnuhKdgqBPyiPQSyBjlhZgS0qqZ1jgVhjGVddyCMWDC6kBP+FjgZQaxKu6wLFQU10GvQXsWlWeBrQwqaeqqoClXjP+lIciyuYE0Yuj+mWZIACrI5Da15Arssq3bngAdpQZZYpcPlApHOoIKA/wVEraFCezdhZbbX6xkCW7M7Yg94uvPGtqzCmrBbsmtaAoInKICelkY5B5EECOWhdj9AR0TNRKtCbXevkluJ4l7la1alZdlCbhmXujh4wuluuXIMOYSrlD7LonOtCbvhh71KRy0iLQlOwukW8HTSnrkuKFedEYne0HW6sEho0bbBDKqEcJgReJEYws6922s+teNpywzrtqQjuW10zYfgOs0EL4eDXkPHdLf88UJdeB2WgE9ih0t9WAd4oQSfh+yVPmjgN+77fiAaqUZC8jyTa0DZtCQLJWvSZ+JCJF+JdkIYj2ctPiRJHPALPF4p5VphXc/AIlpCuCf/ivbkeW4otdXMFsRzIfq+mohuHVgBKnNooXBlULLLpr4LeCwPyLyIA1B94QhCJyDxS2Dw7m7QP3+JAmEgo2goe2YMyvgwmfVEEsNBH1dVQGf5BNpmxAd5EuaUhJzg+FF3vp813CzshkO33+Z0UUy+w7LgohaO5tjhd5UhrbXMFtwdeXx/9py6vlU66T98qp4a9HCubgAAAABJRU5ErkJggg=='
const GITHUB_DARK =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAHdSURBVHgBvVeLbYNADDVdoHSDG4ENwgjpBMkG6QbtBkknIBtEnaDdADoBbEA6gWuXQ3GR7wOc8qSnSPhvkzuTQSQQMaefLXFDLIiGmFvxldgRG+IX8SPLsiukAAU2xDOxx3mo2BaWgismHnE9jrZ7s6tuMR1ajO0GKRaYNrhMorh35VoSBjwzbxWDCy5Lil/ailgrPnMtAe2F2wn5Hm+J9Nbxp2AvZNKuVPwetdZrKJQuGfCPMI/0baTSOai0EJ4EKlmVC3tYCdRHwOBR5Q8wHK8aOhiO1bVorK8phqPd0/4dJIKnCxULa4dw3vEZTkK7S2oegVH0u2S3mfCpPDOcQNJKPdAK+nsJ7wW10Ixn4xA+pRwDD1x5fOUOdA6bDaQLXjpEHSfw7RC+QDrsHc+b8ZJxYXUS5OPg8b8bj2LfvvcKCxEIzshHRXkavhG3+H8HaK2zMiJoYXXrQPBKGplJsK3tjObk4gl+wXiYqfFJCHublLailZ4EysjgJ814upKdRHc4uYp4gPAIQmjRdc8oFRcwExHBjdTPFAcclOc8Kp5huNN/iI90Or5DIAGHqCM+k30DEVU41/MI26jKo4DDX3JtAidcs1vg7eM0NoEeb98EBlIBAyu50DNzKv4F8Mg9kvfoSM0AAAAASUVORK5CYII='
/** Synthetic control: a mid-tone brand colour, the shape of icon that reads on
 *  either theme and must therefore come back untouched from both. */
const MID_TONE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAnElEQVR42u2XSw6AIAxEZeJOj6HnFM+px9C17g2/1raSCNuGeUNJYNp1f1+OuuHw05Wqj353KgZyYK4RJw2mGoEmvEQDmvASLWjDc5qwgKe0e47QsGzR2rnOJC1YnT7GwNcvISxPH2LV04FmoBloBqhJRioj1HUFFl14MlBlKtb6F0IdhkS0fhNOIZXvuckYkkMGR6P+wUR7NGvrBkbrSGNC5zd3AAAAAElFTkSuQmCC'

/** Wire payloads keyed by URL, as `GET /api/link-meta` would answer them. */
const META: Record<string, { title: string; icon: string; icon_dark: string }> = {
  'https://github.com/kirodotdev/KiroCrew/pull/843': {
    title: 'feat(agents): add Agent Template creation',
    // One icon for every surface: the reported bug, and the common case.
    icon: GITHUB_LIGHT,
    icon_dark: '',
  },
  'https://declares-a-variant.example.com/post': {
    title: 'Site that declares a dark-scheme icon',
    icon: GITHUB_LIGHT,
    icon_dark: GITHUB_DARK,
  },
  'https://mid-tone.example.com/post': {
    title: 'Mid-tone brand colour, legible either way',
    icon: MID_TONE,
    icon_dark: '',
  },
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/link-meta')) {
    const target = decodeURIComponent(new URLSearchParams(url.split('?')[1] || '').get('url') || '')
    const meta = META[target]
    if (!meta) {
      return Promise.resolve(
        new Response(JSON.stringify({ code: 'fetch_failed' }), { status: 502 }),
      )
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          url: target,
          title: meta.title,
          description:
            'A page description long enough to show the card’s two-line clamp under the title.',
          site_name: 'Example',
          domain: new globalThis.URL(target).hostname.replace(/^www\./, ''),
          icon: meta.icon,
          icon_dark: meta.icon_dark,
          fetched_at: 1770000000,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const CHIPS = [
  'Opened [feat(agents): add Agent Template creation](https://github.com/kirodotdev/KiroCrew/pull/843) is the one from the report.',
  '',
  'A site that declares its variant: [Site that declares a dark-scheme icon](https://declares-a-variant.example.com/post) mid-sentence.',
  '',
  'And a mid-tone mark: [Mid-tone brand colour, legible either way](https://mid-tone.example.com/post) mid-sentence.',
].join('\n')

const CARD = 'https://github.com/kirodotdev/KiroCrew/pull/843'

function Scene() {
  return (
    <div data-capture-root className="bg-bg p-5" style={{ width: 720 }}>
      <MarkdownRenderer content={scene === 'card' ? CARD : CHIPS} linkPreviews />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Scene />
    </QueryClientProvider>
  </MemoryRouter>,
)
