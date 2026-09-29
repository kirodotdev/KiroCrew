/**
 * Prompt-length check for the chat composer.
 *
 * The model's context window, in tokens, decides whether a prompt can be
 * delivered whole. `POST /api/chat` (`api_chat` in
 * `src/kiro_crew/dashboard/chat_handlers.py`) applies no per-message cap of its
 * own, so the window is the first limit a large prompt meets. The composer
 * already receives it as `contextWindowTokens` (the same value the context
 * meter shows). An unknown or invalid value skips this check.
 *
 * The count is taken over the text the send path actually posts: collapsed
 * paste chips expanded back to their content (see `expandAll` in
 * `utils/pasteTokens.ts`), not the visible editor text.
 *
 * Tokens are ESTIMATED, not tokenized: a quarter token per ASCII character and
 * one token per other code point. That is close for English and code, and
 * errs high for CJK and other scripts, so the warning fires early rather than
 * late. Every figure the UI shows is therefore prefixed with "~".
 */

import { expandAll, type PasteBlock } from '../utils/pasteTokens'

/** Fraction of the context window at which the composer shows the indicator. */
export const PROMPT_LENGTH_WARN_RATIO = 0.9

export type PromptLengthLevel = 'ok' | 'near' | 'over'

export interface PromptLengthCheck {
  level: PromptLengthLevel
  /** The model context window in tokens; 0 when unknown. */
  limit: number
  /** The estimated prompt size in tokens. */
  used: number
  /** How far over the window the prompt is; 0 when not over. */
  overBy: number
  /** used / limit; 0 when the window is unknown. */
  ratio: number
}

/**
 * Estimate tokens by walking code units directly, so a multi-megabyte paste
 * costs no allocation per keystroke.
 */
export function measurePrompt(text: string): number {
  let ascii = 0
  let other = 0
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (c < 0x80) {
      ascii++
    } else {
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
        const next = text.charCodeAt(i + 1)
        if (next >= 0xdc00 && next <= 0xdfff) i++
      }
      other++
    }
  }
  return Math.ceil(ascii / 4) + other
}

/** The trimmed text the backend would receive after paste-chip expansion. */
export function sentPromptText(value: string, blocks: readonly PasteBlock[]): string {
  const expanded = blocks.length ? expandAll(value.trim(), blocks as PasteBlock[]) : value
  return expanded.trim()
}

/** Classify an estimated prompt size against the model context window. */
export function checkPromptLength(
  tokens: number,
  contextWindowTokens: number | undefined,
): PromptLengthCheck {
  const limit = Number.isFinite(contextWindowTokens) && (contextWindowTokens as number) > 0
    ? (contextWindowTokens as number)
    : 0
  const used = tokens
  const ratio = limit ? used / limit : 0
  const level: PromptLengthLevel = !limit
    ? 'ok'
    : used > limit ? 'over' : ratio >= PROMPT_LENGTH_WARN_RATIO ? 'near' : 'ok'
  return { level, limit, used, overBy: limit ? Math.max(0, used - limit) : 0, ratio }
}
