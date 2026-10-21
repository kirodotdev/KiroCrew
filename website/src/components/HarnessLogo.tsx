// Each agent harness's own brand mark, for the Agent Harness settings list.
//
// The marks are static brand assets imported as plain URLs (no svgr, no inline
// SVG paths in TSX), the same carve-out from `use-lucide-icons` that the other
// brand marks (`BrandIcon.tsx`, `KiroGhostMark.tsx`) rely on. Each renders through
// `BrandGlyph`, which paints the file as a CSS mask over `currentColor`: one colour
// that follows the light/dark theme and the row's muted text, whatever colour the
// source art was drawn in.
//
// Where each file came from and under what terms is recorded in
// THIRD-PARTY-NOTICES ("Brand marks"). A harness with no mark here keeps the
// generic `Boxes` glyph, so a harness the server starts listing needs no edit.
import { Boxes } from 'lucide-react'
import { KIRO_HARNESS_BACKENDS } from '../api/acpBackend'
import { BrandGlyph } from './BrandIcon'
import { KiroGhostMark } from './KiroGhostMark'
import claudeMarkUrl from '../assets/harness-logos/claude.svg'
import deepseekMarkUrl from '../assets/harness-logos/deepseek.svg'
import gooseMarkUrl from '../assets/harness-logos/goose.svg'
import openaiMarkUrl from '../assets/harness-logos/openai.svg'
import opencodeMarkUrl from '../assets/harness-logos/opencode.svg'
import piMarkUrl from '../assets/harness-logos/pi.svg'

/**
 * Brand mark per harness id, keyed by the wire id the server sends. Kiro's two
 * harnesses (`kiro` = Kiro CLI, `kas` = kiro-agent) are drawn by `KiroGhostMark`
 * below rather than listed here, so they share the one Kiro asset.
 */
const HARNESS_MARK_URL: Readonly<Record<string, string>> = {
  claude: claudeMarkUrl,
  codex: openaiMarkUrl,
  deepseek: deepseekMarkUrl,
  goose: gooseMarkUrl,
  opencode: opencodeMarkUrl,
  pi: piMarkUrl,
}

const KIRO_HARNESSES: ReadonlySet<string> = new Set(KIRO_HARNESS_BACKENDS)

/**
 * Decorative harness mark (aria-hidden): the row's label stays its accessible name.
 *
 * @param id    Harness wire id (`kiro`, `kas`, `claude`, `codex`, ...).
 * @param size  Box edge in px (default 14, matching the list's other glyphs).
 */
export function HarnessLogo({ id, size = 14 }: { id: string; size?: number }) {
  if (KIRO_HARNESSES.has(id)) return <KiroGhostMark size={size} />
  const url = Object.hasOwn(HARNESS_MARK_URL, id) ? HARNESS_MARK_URL[id] : undefined
  if (url) return <BrandGlyph url={url} size={size} testId={`harness-logo-${id}`} />
  return <Boxes size={size} aria-hidden="true" data-testid="harness-logo-fallback" />
}
