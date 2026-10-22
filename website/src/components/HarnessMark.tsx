import { Cpu } from 'lucide-react'
import { BrandGlyph } from './BrandIcon'
import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, acpBackendName } from '../api/acpBackend'
import kiroGhostMarkUrl from '../assets/kiro-ghost-mark.svg'
import claudeMarkUrl from '../assets/claude-mark.svg'
import codexMarkUrl from '../assets/codex-mark.svg'

/** Monochrome mark per ACP backend id, painted over `currentColor` by `BrandGlyph`.
 *
 *  kiro-cli and KAS are Kiro's own harnesses and share the ghost the nav rail
 *  already uses. `claude-mark.svg` is the CC0 simple-icons Claude glyph, the
 *  source of the Connections marks; simple-icons carries no OpenAI or Codex mark,
 *  so `codex-mark.svg` is the MIT lobe-icons Codex glyph, its notice inside the
 *  file. The marks remain their owners' trademarks, used only to name the harness.
 */
const HARNESS_MARKS: Record<string, string> = {
  [ACP_BACKEND_KIRO]: kiroGhostMarkUrl,
  [ACP_BACKEND_KAS]: kiroGhostMarkUrl,
  claude: claudeMarkUrl,
  codex: codexMarkUrl,
}

/** The harness a live session runs on, as a logo whose accessible name and
 *  tooltip are the harness name. One without a mark takes the Agent settings
 *  tab's glyph. */
export default function HarnessMark({ backend, size = 13 }: { backend: string; size?: number }) {
  const name = acpBackendName({ id: backend })
  const mark = HARNESS_MARKS[backend]
  return (
    <span role="img" aria-label={name} title={name} className="inline-flex shrink-0 opacity-70" data-testid="composer-model-chip-backend">
      {mark
        ? <BrandGlyph url={mark} size={size} testId={`harness-mark-${backend || 'kiro'}`} />
        : <Cpu size={size} aria-hidden="true" />}
    </span>
  )
}
