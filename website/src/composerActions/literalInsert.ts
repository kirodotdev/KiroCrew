/**
 * Putting an app's text into the composer as plain, visible text.
 *
 * An app's text goes only into an EMPTY composer, so the text is the whole draft,
 * and only when nothing in it would be read as more than text
 * (`composerWouldResolve`). Otherwise the host refuses it. Accepted text carries no
 * mark: what the person types after it is their own and resolves normally.
 *
 * @module composerActions/literalInsert
 */
import { IMG_EXT, parseDirs, parseFiles } from '../utils/fileTokens'
import { PASTE_TOKEN_REGEX, type PasteBlock } from '../utils/pasteTokens'

/* Each rule is a SUPERSET of a reader, so whatever that reader resolves, the
   check refuses. The gateway's readers are Python, so the client cannot call them;
   each rule names the reader it covers and the wider shape it tests instead. */

/** A line whose first non-blank character is `/`: a slash command or quick prompt
 *  (`chat_turn/slash_commands.py`, `first_word = message.split()[0]`; the client's
 *  `interceptSlashCommand` reads `raw.trim()`). Blank is JS `\s` (what `trim()`
 *  removes) plus U+0085, which Python's `split()` and `strip()` also remove
 *  (`displaySafe` already drops U+001C to U+001F). */
const LINE_SLASH_RE = /^[\s\u0085]*\//gmu
/** A line whose first non-blank character is `!`: the composer's terminal-command
 *  reader (`useTerminalCommand.terminalCommand`, a separated leading bang) is
 *  narrower, and a terminal command reaches the person's shell. */
const LINE_BANG_RE = /^[\s\u0085]*!/gmu
/** `@` anywhere but straight after a letter or digit (so `a@b.com` stays text):
 *  file and folder mentions (`fileTokens.leadingMentionBoundary`), a leading
 *  `@prompt` or `@knowledge`. */
const AT_RE = /(?<![\p{L}\p{N}])@/gu
/** `$` before a letter or digit, after anything: the gateway's `$skill` reader
 *  (`skills._DOLLAR_SKILL_PATTERN`, `(?<![\w$])\$[a-z0-9]`) is narrower. */
const DOLLAR_RE = /\$[a-z0-9]/giu
/** A path separator followed on the same line by an image suffix. Every path the
 *  gateway's image upgrade matches (`kiro_crew.image_refs._PATH_RE`, POSIX or
 *  Windows) starts with a separator and ends in one of these suffixes on one line. */
/** Markdown image syntax, any raw HTML tag, and any attachment or folder marker,
 *  however spaced or encoded: the transcript renderer passes raw HTML through
 *  (`rehypeRaw`) and fetches a local image, audio or video destination from either
 *  form (`ImgWithFallback`, `remoteMedia`, which decode it first), and
 *  `resolveFileSegment` / `resolveDirSegment` read markers with any blank after them.
 *  Every tag is refused, not only media tags, so no tag the renderer learns later
 *  can carry a destination through. */
const RENDERED_REF_RE = /!\[|<[\p{L}!/?]|\[attached_(?:file|dir)\b/iu
const IMAGE_PATH_RE = new RegExp(`[/\\\\][^\\n\\r\\u2028\\u2029]*?${IMG_EXT.source.replace(/\$$/, '')}`, 'giu')

const matches = (re: RegExp, text: string): boolean => { re.lastIndex = 0; return re.test(text) }

/** Whether anything in `draft` would be read as more than text by the composer or
 *  the gateway: a command, a mention, a skill, a staged-paste token, an attachment
 *  or folder marker, an image path. */
export function composerWouldResolve(draft: string): boolean {
  return matches(LINE_SLASH_RE, draft)
    || matches(LINE_BANG_RE, draft)
    || matches(AT_RE, draft)
    || matches(DOLLAR_RE, draft)
    || matches(PASTE_TOKEN_REGEX, draft)
    || parseFiles(draft).length > 0
    || parseDirs(draft).length > 0
    || matches(IMAGE_PATH_RE, draft)
    || RENDERED_REF_RE.test(draft)
}

/** Code points that draw nothing (zero-width, tag characters, variation
 *  selectors, fillers): text built from them would reach the agent unseen. */
const INVISIBLE_RE = /\p{Default_Ignorable_Code_Point}/gu

/** `text` with every code point that draws nothing removed, so the draft holds
 *  only what the person can read before sending. */
export function visibleOnly(text: string): string {
  return text.replace(INVISIBLE_RE, '')
}

/** Whether the composer holds nothing of the person's: no text, no collapsed paste.
 *  Only such a composer takes an app's text. */
export function draftIsEmpty(value: string, pasteBlocks: readonly PasteBlock[]): boolean {
  return value.trim() === '' && pasteBlocks.length === 0
}
