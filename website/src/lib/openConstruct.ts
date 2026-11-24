/**
 * Whether streaming text ends inside a construct that renders only whole: an
 * `<mcwidget>`, a code fence or an `[OPTIONS:` marker still being written.
 *
 * The browser half of the gateway's `open_construct_at_end`. A mid-turn steer
 * that lands inside one does not cut the reply on either side: the gateway
 * holds its segment cut until the construct closes, and the transcript leaves
 * the streaming message live above the steer bubble so the construct keeps
 * growing where it started instead of breaking into raw text below it.
 *
 * Reuses the renderer's own parsers, so a tag quoted in inline code or inside
 * a fence does not read as an open widget, and a half-typed marker head is
 * judged by the same rule that hides it while it streams.
 */
import { parseBlocks } from '../hooks/useBlockAssembler'
import { stripPartialOptionMarker } from '../app-sdk/protocol/optionMarker'

export function endsInOpenConstruct(text: string): boolean {
  if (!text) return false
  if (stripPartialOptionMarker(text) !== text) return true
  const blocks = parseBlocks(text, true)
  const last = blocks[blocks.length - 1]
  return !!last && !last.complete
}
