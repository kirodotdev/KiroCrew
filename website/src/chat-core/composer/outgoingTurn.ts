/**
 * The outgoing turn: what one composer submit puts on the wire and in the
 * transcript (chat-core RFC §3 layer 4, P3-c).
 *
 * A composer holds more than its text: staged files, `@rel/` folder tokens,
 * collapsed `[ Paste #N ]` tokens and their blocks, dropped session
 * references, a picked knowledge block, a quoted message. Each becomes part of
 * the message in one fixed order, and this module is the one place that order
 * is written down, for every host that submits:
 *
 *   1. staged files: images become leading `![image](path)` lines, other files
 *      `[attached_file N] path` markers (`prepareSendPayload`);
 *   2. folder tokens: `@rel/` becomes `[attached_dir N] /abs/path` on the wire
 *      while the bubble keeps the token for its chip, and `meta.dirs[N-1]` is
 *      marker N's path (send only). Absolute, so the reference survives a
 *      cwd/project mismatch and history replay; after the file pass, which is
 *      disjoint because a file token never ends in `/`;
 *   3. session references: a link per reference, appended to both texts (send
 *      only) -- deliberately a POINTER, never the referenced transcript, which
 *      would spend a large share of this session's context window in one turn
 *      and can trip autocompact. The agent follows the link through a read
 *      path that is bounded, redacted and incognito-refusing server-side. The
 *      link is the session menu's "Copy link" string, and appending (never
 *      splicing) leaves earlier paste-token ranges untouched;
 *   4. collapsed pastes: each token whose block is staged AND whose token is
 *      still in the text expands on the wire; the bubble keeps the token and
 *      `meta.pastes` carries the blocks so it draws a chip. On a send the
 *      bubble text decides which tokens are live, so a block whose token left
 *      the text expands nowhere, not even inside a file or folder marker whose
 *      path happens to spell its token. On the text-only channel the wire is
 *      the only text, so a token there counts wherever it stands;
 *   5. a knowledge block opens the wire text (send only);
 *   6. a quoted message opens both texts, so the bubble's card can strip
 *      exactly what was sent. Last, so it stays the very first thing in the
 *      text: the bubble strips the block only from position 0
 *      (`stripQuoteBlock`), and a knowledge block in front of it would leave
 *      the card AND the raw block on screen.
 *
 * The order is the contract. Folder and paste passes run before anything is
 * prepended, and pasted content expands after the file and folder passes, so
 * pasted, quoted or knowledge text is never read as one of this turn's tokens.
 *
 * `'steer'` is the text-only channel (a mid-turn steer, the side chat): no
 * folder serialization (a marker with no `meta.dirs` to replay against would
 * truncate a spaced path, while the raw `@rel/` token stays correct under
 * replay -- serialize there only if that transport ever carries attachment
 * metadata), no session links or knowledge, and the bubble IS the wire text,
 * since nothing travels beside it to re-collapse a paste.
 *
 * Pure: no store, no side effect. The paste side-table write the bubble needs
 * on history load is returned as `storedPaste` for the host to perform.
 */
import { prepareSendPayload, serializeDirTokens } from '../../utils/fileTokens'
import { appendSessionRefLinks, type SessionRef } from '../../utils/sessionRefs'
import { expandAll, pruneBlocks, type PasteBlock } from '../../utils/pasteTokens'
import { expandKnowledgeBlock, type KnowledgeBlock } from '../../pages/chat/useKnowledgeFetch'
import { prependQuote, type MessageQuote } from './messageQuote'

export type OutgoingTurnMode = 'send' | 'steer'

/** What the composer is submitting. Omitted fields are empty. */
export interface OutgoingTurnInput {
  /** The composer text, tokens included. Trimmed here. */
  text: string
  /** Staged attachment paths, in staging order. */
  files?: readonly string[]
  /** The blocks behind the text's `[ Paste #N ]` tokens. */
  pastes?: readonly PasteBlock[]
  /** Staged session references (send only). */
  sessionRefs?: readonly SessionRef[]
  /** A picked knowledge block (send only). */
  knowledge?: KnowledgeBlock | null
  quote?: MessageQuote | null
  /** Project root `@rel/` folder tokens resolve against (send only). */
  project?: string
}

/** The knowledge card's record on `meta.knowledge`. */
export interface KnowledgeMeta {
  items: number
  tokens: number
  titles: string[]
  content: Array<{ title: string; text: string }>
}

/** The fields this module contributes to the message's `meta`, in wire order. */
export interface OutgoingTurnMeta {
  /** Non-image attachments; `[attached_file N]` names `files[N-1]`. */
  files?: string[]
  /** Folder paths; `[attached_dir N]` names `dirs[N-1]` (send only). */
  dirs?: string[]
  /** The expanded blocks, so the bubble draws chips (send only). */
  pastes?: PasteBlock[]
  quote?: MessageQuote
  knowledge?: KnowledgeMeta
}

/** Arguments for `saveStoredPaste`, so history load can re-collapse the server's expanded echo. */
export interface StoredPasteEntry {
  expanded: string
  display: string
  pastes: PasteBlock[]
  files: string[]
}

export interface OutgoingTurn {
  /** What the agent receives. */
  wire: string
  /** The optimistic bubble's text. */
  bubble: string
  meta: OutgoingTurnMeta
  /** The staged blocks this turn expanded: those whose token is in the text.
   *  What a pane or side-chat refusal hands back with the text (the main chat
   *  hands back every staged block). */
  pastes: PasteBlock[]
  /** The text with only the staged files inlined (image lines and
   *  `[attached_file N]` markers): folder and paste tokens untouched, no link,
   *  knowledge or quote. What the main chat's failed send hands back. */
  inlined: string
  /** Present on a send that expanded a paste. */
  storedPaste?: StoredPasteEntry
  /** The wire is the typed text with only its file and folder markers and the
   *  quote: no paste was expanded, no session link appended and no knowledge
   *  prepended. A queued send whose wire has more than that cannot be put back
   *  from the typed text and files alone. */
  typedOnly: boolean
}

/** The knowledge card's record for a picked block (content capped per item). */
export function knowledgeMeta(block: KnowledgeBlock): KnowledgeMeta {
  return {
    items: block.items.length,
    tokens: block.totalTokens,
    titles: block.items.map(i => i.title),
    content: block.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })),
  }
}

/** True when the submit carries nothing: no text, no quote, no file, no
 *  session reference. A knowledge block or a project alone is not a payload,
 *  and a paste is one only through its token in the text. */
export function isEmptyTurn(input: OutgoingTurnInput): boolean {
  return !input.text.trim() && !input.quote && !input.files?.length && !input.sessionRefs?.length
}

export function buildOutgoingTurn(input: OutgoingTurnInput, mode: OutgoingTurnMode): OutgoingTurn {
  const send = mode === 'send'
  const quote = input.quote ?? null
  const { txt, displayTxt, filePaths } = prepareSendPayload(input.text.trim(), [...(input.files ?? [])])
  const { llm: typedWire, dirPaths } = send ? serializeDirTokens(txt, input.project || '') : { llm: txt, dirPaths: [] }
  const refs = send ? [...(input.sessionRefs ?? [])] : []
  const linked = appendSessionRefLinks(typedWire, refs)
  // On a send the bubble text is what the user sees, so it decides which
  // tokens are live; on the text-only channel the wire is the only text.
  const display = send ? appendSessionRefLinks(displayTxt, refs) : linked
  const pastes = pruneBlocks(display, (input.pastes ?? []) as PasteBlock[])
  let wire = pastes.length ? expandAll(linked, pastes) : linked
  const knowledge = send ? input.knowledge ?? null : null
  if (knowledge) wire = expandKnowledgeBlock(knowledge) + '\n' + wire
  if (quote) wire = prependQuote(wire, quote)
  const bubble = !send ? wire : quote ? prependQuote(display, quote) : display
  const meta: OutgoingTurnMeta = {}
  if (filePaths.length) meta.files = filePaths
  if (dirPaths.length) meta.dirs = dirPaths
  if (send && pastes.length) meta.pastes = pastes
  if (quote) meta.quote = quote
  if (knowledge) meta.knowledge = knowledgeMeta(knowledge)
  return {
    wire,
    bubble,
    meta,
    pastes,
    inlined: txt,
    ...(send && pastes.length ? { storedPaste: { expanded: wire, display: bubble, pastes, files: filePaths } } : {}),
    typedOnly: wire === (quote ? prependQuote(typedWire, quote) : typedWire),
  }
}
