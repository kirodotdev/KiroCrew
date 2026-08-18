/**
 * Rendered ` ```mermaid ` block for the Notes preview.
 *
 * `Preview` is a synchronous line-to-block mapper, while mermaid's API is
 * async twice over: the library itself is a lazy chunk (~100 KB gzipped that
 * most notes never need), and `render()` returns a promise. This component is
 * the escape hatch: it mounts showing the diagram SOURCE — the exact thing the
 * note showed before this feature existed — and swaps in the SVG when it
 * arrives, so a note without diagrams pays nothing and a slow load degrades to
 * the old behaviour instead of a blank hole.
 *
 * A failed render keeps the source visible and adds a one-line hint rather
 * than replacing the block with an error box: in a click-to-edit notebook the
 * source IS the recovery path, and the user's very next gesture (click, fix
 * the typo) needs the text in front of them.
 *
 * Colours stay on mermaid's built-in `dark`/`default` themes on purpose. The
 * dashboard accent is not used: theme packs may set `--accent` to any value —
 * including one indistinguishable from the background — and mermaid derives
 * dozens of fills from each seed colour, so a bad accent would not degrade to
 * faint chrome (the headings compromise) but to unreadable diagrams. The
 * built-in themes are contrast-safe on the card background in both modes.
 */
import { useEffect, useState } from 'react'
import { sanitize } from '../../api/helpers'
import { FONT_MONO } from './constants'
import { mermaidErrorLabel } from './labels'

/**
 * Strip external references from sanitized SVG markup to prevent exfiltration.
 *
 * Mermaid's image-node syntax (`:::img[url]`) can produce `<image href="...">`
 * elements pointing to attacker-controlled URLs. DOMPurify preserves them
 * (valid SVG element) and CSP permits HTTPS, so opening the note would fire
 * an attacker-controlled request.
 *
 * This post-processor removes `<image .../>` elements entirely, and strips
 * href/xlink:href attributes whose values don't start with `#` (preserving
 * internal fragment references used by mermaid for markers and defs).
 */
function stripExternalSvgRefs(svgMarkup: string): string {
  // Remove <image> elements (self-closing or with end tag).
  let result = svgMarkup.replace(/<image\b[^>]*\/?\s*>(\s*<\/image>)?/gi, '')
  // Strip href/xlink:href attributes pointing to external origins.
  result = result.replace(
    /\b(xlink:href|href)\s*=\s*"(?!#)([^"]*)"/gi,
    '',
  )
  // Strip CSS fetch functions that reference external origins in style
  // attributes and <style> blocks. Covers url(), image-set(), image(), and
  // src() — all CSS functions capable of triggering a network request.
  // Neutralize ALL values that don't reference a local fragment (#id).
  // This prevents exfiltration via e.g. classDef with
  // mask-image:image-set('https://attacker/pixel' 1x).
  //
  // url() uses a tight character class (no nested parens); the others can have
  // commas, spaces, and nested url() calls, so we match balanced parens.
  result = result.replace(
    /url\(\s*['"]?(?!['"]?#)([^'")]+)['"]?\s*\)/gi,
    'url()',
  )
  // image-set(), image(), src() — match the function name and everything up to
  // its balanced closing paren (these can contain quotes, commas, resolution
  // descriptors). We replace the entire function call with an empty url().
  result = result.replace(
    /(?:image-set|image|src)\([^)]*(?:\([^)]*\)[^)]*)*\)/gi,
    'url()',
  )
  // Handle CSS escape sequences (\XX hex) that could disguise function names
  // to bypass the above pattern (e.g. \75rl() for url()).
  result = result.replace(
    /\\[0-9a-f]{1,6}\s*/gi,
    '',
  )
  return result
}

interface MermaidApi {
  initialize: (config: Record<string, unknown>) => void
  render: (id: string, code: string) => Promise<{ svg: string }>
}

/** Lazy singleton: one chunk fetch no matter how many diagrams a note holds. */
let mermaidLoad: Promise<MermaidApi> | null = null
function loadMermaid(): Promise<MermaidApi> {
  mermaidLoad ??= import('mermaid').then(m => m.default as unknown as MermaidApi)
  return mermaidLoad
}

/** `mermaid.render` demands a document-unique id for its work element. */
let seq = 0

/** Same dark detection as the chat renderer, so both surfaces agree. */
function isDarkTheme(): boolean {
  return (document.documentElement.getAttribute('data-theme') ?? '').includes('dark')
}

const FRAME_STYLE: React.CSSProperties = {
  background: 'var(--card)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  padding: '10px',
  overflowX: 'auto',
}

const SOURCE_STYLE: React.CSSProperties = {
  ...FRAME_STYLE,
  fontSize: '12px',
  fontFamily: FONT_MONO,
  margin: 0,
}

export function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  // Bumped when the dashboard theme changes, so open notes re-render their
  // diagrams in the new palette instead of keeping stale colours until the
  // next edit.
  const [themeTick, setThemeTick] = useState(0)

  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick(t => t + 1))
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => obs.disconnect()
  }, [])

  useEffect(() => {
    let cancelled = false
    setFailed(false)
    loadMermaid()
      .then(mermaid => {
        // Re-initialized on every render pass — mermaid keeps global config,
        // and this is what lets a theme switch actually take effect.
        //
        // htmlLabels is forced OFF: with it on, mermaid puts node labels in
        // <foreignObject> wrappers, which DOMPurify's default profile strips —
        // the sanitize() below would silently eat every label. Plain SVG
        // <text> labels survive sanitization and render identically here.
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          suppressErrorRendering: true,
          fontFamily: 'inherit',
          theme: isDarkTheme() ? 'dark' : 'default',
          htmlLabels: false,
          flowchart: { htmlLabels: false },
        })
        seq += 1
        return mermaid.render(`mdnb-mermaid-${seq}`, code)
      })
      .then(res => {
        if (!cancelled) setSvg(stripExternalSvgRefs(sanitize(res.svg)))
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [code, themeTick])

  if (svg !== null && !failed) {
    return (
      <div
        style={FRAME_STYLE}
        // Defense in depth: rendered with securityLevel 'strict' upstream,
        // passed through DOMPurify sanitize(), and then stripExternalSvgRefs
        // removes <image> elements + external href attributes.
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    )
  }
  return (
    <div>
      <pre style={SOURCE_STYLE}>{code}</pre>
      {failed && (
        <div style={{ fontSize: '11px', color: 'var(--muted)', padding: '2px 4px' }}>
          {mermaidErrorLabel()}
        </div>
      )}
    </div>
  )
}
