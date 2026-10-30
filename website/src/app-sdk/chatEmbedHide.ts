/**
 * The parts of `ChatEmbed` a host app can hide with its `hide` prop.
 *
 * Kept in a module with no imports so the type test can compile it on its
 * own: an unknown key in `hide` must be a type error, and that is proven by
 * running the compiler over this file rather than by reading it.
 *
 * Hiding only removes. A hidden part is not mounted at all; no key turns on
 * something the embed would not otherwise show (attachments still need the
 * upload grant, approvals still need `/api/chat`, voice input stays off).
 */
export const CHAT_EMBED_HIDE_KEYS = [
  /** The framed title strip (title, agent, running state). */
  'header',
  /** The attach button, plus paste and drop staging of files. */
  'attach',
  /** The Stop button that replaces Send while a turn runs. */
  'stop',
  /** The queued-message cards above the composer. */
  'queue',
  /** The input/output panel a tool row opens on click. */
  'toolDetails',
  /** The follow-up suggestion chips above the composer. */
  'followUps',
] as const

/** One part of `ChatEmbed` that `hide` can remove. */
export type ChatEmbedHideKey = typeof CHAT_EMBED_HIDE_KEYS[number]
