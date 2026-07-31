import type { DisplayItem } from '../pages/chat/types'

/**
 * Geometry + selection helpers for the pinned-prompt banner (the most recent
 * user prompt at or above the chat fold, shown as a sticky band under the
 * session title).
 *
 * These mirror native `position: sticky` stacking semantics, which is what the
 * design calls for: a prompt scrolls with the transcript until its top reaches
 * the fold, sticks there, and is pushed out by the NEXT prompt as that prompt's
 * top border meets it. The banner cannot be a real sticky element because the
 * transcript is virtualized — a row scrolled far above the window unmounts, so
 * the sticky node would vanish. Instead the banner is an overlay driven by the
 * same math.
 */

/** Only user-typed prompts pin. `nudge` opens a turn too but is machine-injected. */
function isPrompt(item: DisplayItem | undefined): boolean {
  return !!item && item.kind === 'single' && item.msg.role === 'user'
}

/**
 * Display index of the prompt that should be pinned, or -1 for none.
 *
 * @param items          the flattened display list
 * @param foldIdx        display index of the first row whose bottom is below the fold
 * @param foldIdxCrossed true when that row's own top has already passed the fold
 *                       (i.e. it is straddling it, not starting below it)
 */
export function findPinnedPromptIdx(
  items: DisplayItem[],
  foldIdx: number,
  foldIdxCrossed: boolean,
): number {
  const start = Math.min(foldIdxCrossed ? foldIdx : foldIdx - 1, items.length - 1)
  for (let i = start; i >= 0; i--) {
    if (isPrompt(items[i])) return i
  }
  return -1
}

/** Display index of the first prompt after `afterIdx`, or -1 if none. */
export function findNextPromptIdx(items: DisplayItem[], afterIdx: number): number {
  for (let i = Math.max(afterIdx + 1, 0); i < items.length; i++) {
    if (isPrompt(items[i])) return i
  }
  return -1
}

/**
 * How far (px) to translate the banner UP so the incoming prompt pushes it out.
 *
 * The banner's bottom edge tracks the incoming prompt's top edge exactly, so the
 * two never overlap: the push starts when the incoming top reaches the banner's
 * bottom (`gap === bannerH`) and completes when it reaches the fold (`gap === 0`),
 * at which point the caller swaps the banner to the incoming prompt.
 *
 * @param nextTop viewport-relative top of the incoming prompt row, or null when
 *                that row is not mounted (i.e. still far below the fold)
 */
export function computePinPush(bannerH: number, foldY: number, nextTop: number | null): number {
  if (nextTop == null || bannerH <= 0) return 0
  const gap = nextTop - foldY
  if (gap >= bannerH) return 0
  return Math.max(0, Math.min(bannerH, bannerH - gap))
}

/**
 * Flatten a prompt to one line of plain text for the collapsed banner.
 * Attachment/image markdown carries no meaning at a glance, so images are
 * dropped, `[attached_file N] /abs/path` collapses to the basename, and fenced
 * code becomes an ellipsis.
 */
export function promptPreview(content: string): string {
  return content
    .replace(/```[\s\S]*?```/g, ' … ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[attached_file \d+\]\s*(\S+)/g, (_m, p: string) => p.split('/').pop() || '')
    .replace(/\s+/g, ' ')
    .trim()
}
