import { useState, useRef, useEffect, memo } from 'react'
import { AnimatePresence, motion, useMotionValue, useSpring } from 'framer-motion'
import { Hourglass, ChevronUp, X, Zap, Pencil, Check, Bot, Loader2, ArrowUp, ArrowDown, AppWindow, BookOpen } from 'lucide-react'
import type { ChatMessage } from '../types'
import { useImeGuard } from '../hooks/useImeGuard'
import { Glass } from './Glass'

import { i18nT } from '../i18n/t'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { stripAppEnvelope } from '../pages/chat/groupDisplayItems'
import { hasSubagentCompletionPrefix } from '../pages/chat/subagentCompletion'
import { prependQuote, quoteBlock, quoteExcerpt, readMessageQuote, stripQuoteBlock } from '../chat-core/composer/messageQuote'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { splitKnowledgeEnvelope } from '../pages/chat/useKnowledgeFetch'
import { expandAll, findTokenRanges, foldPastesInOrder, pruneBlocks, saveStoredPaste, type PasteBlock } from '../utils/pasteTokens'
import { usePasteTokens } from '../chat-core/composer/pasteEditor'
/** System-injected sub-agent completion deliveries waiting for the busy slot.
 *  These are NOT user messages: they must not be editable/cancellable (either
 *  would silently lose a finished agent's result) and rendering each as a
 *  queue card is noise at scale — they collapse into one progress line
 *  (SubagentDeliveryProgress) instead of the interactive QueueStack. */
export function isSystemDelivery(m: ChatMessage): boolean {
  return hasSubagentCompletionPrefix(m.content || '')
}

/** A queued entry that must NOT render as an interactive (edit/cancel) user
 *  card. Two families qualify, both machine orchestration rather than user
 *  speech:
 *    - sub-agent completion deliveries (isSystemDelivery), and
 *    - synthetic turn-recovery continuations (tool refusal / stalled turn /
 *      stalled tool / interrupted / empty response), which the gateway
 *      re-queues automatically and which surface as a compact RecoveryCard in
 *      the transcript once dequeued.
 *  Editing or cancelling either would corrupt an automatic effect, so they are
 *  filtered out of the QueueStack (sub-agent deliveries are still counted for
 *  the progress line via isSystemDelivery). */
export function isNonInteractiveQueued(m: ChatMessage): boolean {
  return isSystemDelivery(m) || parseRecoveryMessage(m.content || '') !== null
}

/** An MCP-App ui/message entry waiting in the queue. It stays VISIBLE in the
 *  stack (the user's own click, awaiting delivery) but read-only: editing its
 *  card would drain the user's replacement words as app-authored (inject row,
 *  actor `app`, channel mirror suppressed) — the backend refuses the edit
 *  (queue_edit_by_id), so the card must not offer it. The `meta.kind` tag
 *  rides on both the live twin row and the slot-detail queue hydration. */
export function isAppMessageQueued(m: ChatMessage): boolean {
  return m.meta?.kind === 'mcp_app_message'
}

/** Display text for a queued entry: an app-message entry drops its machine
 *  envelope so the queue card wears the same skin the transcript row will —
 *  two skins for one message read as two different messages. */
/** A queued entry's text split into the parts the sender did not type and the
 *  part they did. A send is queued with its WIRE text, which opens with the
 *  quote block (when quoting) and then the `[KNOWLEDGE CONTEXT ...]` envelope
 *  (when a knowledge block was picked) -- model-facing material the user never
 *  typed, so neither belongs on the card or in the editor. `quote` is set only
 *  when the content really starts with that quote's block, and `envelope` is
 *  the knowledge envelope as sent (empty when there is none), kept verbatim so
 *  an edit can put it back and the agent still receives the knowledge.
 *
 *  A collapsed `[ Paste #N ]` token is the third kind of model-only text, but
 *  it expands IN PLACE with no marker around it, so the text alone cannot give
 *  it back. The blocks ride the row as `meta.pastes` (`queueEntryPastes`: the
 *  sending browser's paste side table, the same source the sent bubble is
 *  re-collapsed from), and `own` folds each block's text back to its token
 *  (`recollapsePastes`), so the card shows the token the bubble will show and
 *  the editor opens on it. A row with no blocks shows the text as sent. */
function splitQueuedContent(m: ChatMessage): {
  quote: ReturnType<typeof readMessageQuote>
  envelope: string
  own: string
  pastes: PasteBlock[]
} {
  if (isAppMessageQueued(m)) return { quote: null, envelope: '', own: stripAppEnvelope(m.content), pastes: [] }
  const carried = readMessageQuote(m.meta as Record<string, unknown> | undefined)
  const quote = carried && m.content.startsWith(quoteBlock(carried)) ? carried : null
  const afterQuote = quote ? stripQuoteBlock(m.content, quote) : m.content
  const split = splitKnowledgeEnvelope(afterQuote)
  const expanded = split ? split.rest : afterQuote
  const folded = foldQueuedPastes(expanded, queuedPastes(m))
  return { quote, envelope: split?.head ?? '', own: folded?.own ?? expanded, pastes: folded?.pastes ?? [] }
}

/** How many remains of a `[ Paste #N ]` token `text` holds outside every
 *  whole token: a fence of a token's invisible id run (U+2063, which no
 *  script writes; the id bits between the fences, U+200B and U+200C, are
 *  ordinary characters in Persian and other text and are NOT counted), the
 *  token's head `[ Paste #`, or its tail `· N lines ]`. */
function tokenRemains(text: string, pastes: PasteBlock[]): number {
  let rest = ''
  let pos = 0
  for (const r of findTokenRanges(text, pastes)) {
    rest += text.slice(pos, r.start)
    pos = r.end
  }
  rest += text.slice(pos)
  return (rest.match(/\u2063|\[ Paste #|\u00b7 \d+ lines \]/g) ?? []).length
}

/** The opening blocks whose token stands whole in `text`, in token order:
 *  what an edit of `text` still carries. Derived from the text every time,
 *  never kept as state that shrinks: a native undo puts a deleted token back
 *  as text only, and a list pruned on the delete would then expand nothing
 *  and send the literal token. Only a token carrying its invisible id run
 *  counts; a token-shaped literal with no id run (a body expanded inline that
 *  happens to spell one) never resolves to a block here. */
function liveBlocks(text: string, opened: PasteBlock[]): PasteBlock[] {
  if (!opened.length) return []
  return findTokenRanges(text, opened)
    .filter(r => text.slice(r.start, r.end).includes('\u2063'))
    .sort((a, b) => a.start - b.start)
    .map(r => r.block)
}

/** True when the edit broke a token the editor opened with: one of the
 *  opening blocks is neither whole in the value nor expanded inline in it
 *  (its body absent as well), and the value holds more token remains outside
 *  whole tokens than the text had when the editor opened. The commit expands
 *  only whole tokens, so such a value would send the broken text to the agent
 *  in place of the paste. A token deleted whole leaves no remains; a token
 *  expanded inline leaves its body, literals inside it included, and is not
 *  broken; the comparison against `initial` means what the typed words
 *  already held, or a literal typed beside an intact token, never blocks. */
function hasBrokenToken(value: string, initial: string, opened: PasteBlock[]): boolean {
  if (!opened.length) return false
  const whole = new Set(findTokenRanges(value, opened).map(r => r.block.id))
  const gone = opened.some(b => !whole.has(b.id) && !value.includes(b.content) && !value.includes(b.content.trimEnd()))
  return gone && tokenRemains(value, opened) > tokenRemains(initial, opened)
}

/** Fold a queued entry's paste bodies back to their tokens, or refuse. The
 *  row's blocks are in token order (`queueEntryPastes`), and the fold walks
 *  the text in that order (`foldPastesInOrder`), so a body that is a prefix
 *  of a sibling's lands on its own token: an edit commits by expanding the
 *  tokens the editor shows, so a fold that attributed a body to the wrong
 *  block would send text the user did not choose when they delete a token.
 *  A refused fold (a body missing or out of order) shows the text as sent,
 *  and the editor edits it as plain text, with no blocks to expand. */
function foldQueuedPastes(expanded: string, pastes: PasteBlock[]): { own: string; pastes: PasteBlock[] } | null {
  if (!pastes.length) return null
  const own = foldPastesInOrder(expanded, pastes)
  return own === null ? null : { own, pastes }
}

/** The paste blocks a queued row carries (`queueEntryPastes` put them there). */
function queuedPastes(m: ChatMessage): PasteBlock[] {
  const pastes = m.meta?.pastes
  return Array.isArray(pastes) ? (pastes as PasteBlock[]) : []
}

/** The entry's own words: the content with a carried quote's block and a
 *  knowledge envelope taken off the head, exactly as the sent bubble shows it
 *  (`UserMessage` strips the quote; the envelope is never in the bubble). This
 *  is what the editor opens on; `reglueQuote` puts both back on commit. */
function queuedOwnText(m: ChatMessage): string {
  return splitQueuedContent(m).own
}

/** True when the card hides a knowledge envelope it will still send. The card
 *  then shows only the typed words, so it needs its own mark that knowledge
 *  is attached; without one, the card reads as a plain message. False when the
 *  split refused (two close-marker lines), because then the card shows the
 *  whole text, envelope included. */
function queuedHasKnowledge(m: ChatMessage): boolean {
  return splitQueuedContent(m).envelope !== ''
}

/** The card's one visible line. A quoting entry shows the user's own words,
 *  so two entries quoting the same reply differ; a quote-only send has none,
 *  so the card shows the quoted excerpt rather than a blank line. Preview
 *  only -- the editor seeds from `queuedOwnText`, never from this. */
export function queuedDisplayText(m: ChatMessage): string {
  const own = queuedOwnText(m)
  if (own.trim()) return own
  const quote = readMessageQuote(m.meta as Record<string, unknown> | undefined)
  return quote ? quoteExcerpt(quote.text) : own
}

/** The text an edit of an entry commits: the editor shows and edits the
 *  user's own words (`queuedDisplayText`), and what the entry carries ahead of
 *  them goes back on the head unchanged -- the quote block, so the card
 *  survives the edit, and the knowledge envelope, so the edited send still
 *  carries the knowledge the agent was meant to get. An entry sent with no
 *  typed words (a quote plus knowledge) has its wire trimmed right after the
 *  close marker, so the blank line a fresh send puts between the envelope and
 *  the words is put back here; without it the edit would glue the words onto
 *  the marker line and the envelope would no longer be found.
 *
 *  A `[ Paste #N ]` token the editor showed expands again here, from the
 *  blocks the editor still holds (`blocks`: the row's at open, less any the
 *  editor retired by deleting, cutting or expanding a token inline), so the
 *  agent receives the pasted text and never the bare token, and a token the
 *  editor expanded inline is never expanded a second time; a token the edit
 *  removed takes its paste with it, as deleting the token in the composer
 *  does. Pure: the commit records the edit in the paste side table separately
 *  (`recordEditedPastes`), because this also builds the live draft a side
 *  surface reads on every keystroke. */
function reglueQuote(m: ChatMessage, edited: string, blocks: PasteBlock[] = splitQueuedContent(m).pastes): string {
  const { quote, envelope } = splitQueuedContent(m)
  const live = pruneBlocks(edited, blocks)
  const words = live.length ? expandAll(edited, live) : edited
  const sep = envelope && words && !envelope.endsWith('\n') ? '\n\n' : ''
  const body = envelope + sep + words
  return quote ? prependQuote(body, quote) : body
}

/** The blocks a COMMITTED edit kept, in the order their tokens stand in the
 *  edited text, after recording the edit in the paste side table under the
 *  wire `reglueQuote` built for it. Only on the commit: the live draft runs
 *  through `reglueQuote` on every keystroke, and a record per keystroke, each
 *  holding the whole paste, would push the sent messages' records out of the
 *  table's entry and byte budget. No attachment list goes on the record: the
 *  gateway prunes the entry's list to the markers the edit kept, and a list
 *  recorded here would be put back over it on a reload
 *  (`mergePreservedPastes` prefers the record's `files`). */
function recordEditedPastes(m: ChatMessage, edited: string, wire: string, blocks: PasteBlock[]): PasteBlock[] {
  const { quote } = splitQueuedContent(m)
  const live = findTokenRanges(edited, blocks).sort((a, b) => a.start - b.start).map(r => r.block)
  if (live.length) saveStoredPaste(wire.trim(), quote ? prependQuote(edited, quote) : edited, live)
  return live
}

/** Split a slot's message list into the three things a pane surface needs:
 *  the transcript (everything not queued), the INTERACTIVE queue cards, and a
 *  count of held sub-agent deliveries for the collapsed progress line.
 *
 *  One pass, and one place. Callers own the composer's `input` state, so they
 *  re-render on every keystroke; deriving these in a render body handed the
 *  transcript array a fresh identity per character, which defeated the memo()
 *  on ChatMessageList and re-ran its O(N) turn grouping while the user typed.
 *  Callers must wrap this in a `useMemo` keyed on the input array. */
export function splitPaneMessages(allMessages: ChatMessage[]): {
  messages: ChatMessage[]
  queuedMessages: ChatMessage[]
  systemDeliveryCount: number
} {
  const messages: ChatMessage[] = []
  const queuedMessages: ChatMessage[] = []
  let systemDeliveryCount = 0
  for (const m of allMessages) {
    if (m.role !== 'queued') { messages.push(m); continue }
    // Both queue predicates are independent, not mutually exclusive: a
    // sub-agent delivery is excluded from the interactive stack AND counted
    // for the progress line.
    if (!isNonInteractiveQueued(m)) queuedMessages.push(m)
    if (isSystemDelivery(m)) systemDeliveryCount++
  }
  return { messages, queuedMessages, systemDeliveryCount }
}

/** One quiet, non-interactive line summarizing held sub-agent deliveries —
 *  "the results are in; they'll be processed when the current turn finishes". */
export function SubagentDeliveryProgress({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <div
      // `relative z-[2]`: one explicit layer in the composer dock's status
      // stack, below the composer's own `z-10` like every other bar there, so
      // the stack's paint order is stated rather than left to DOM order.
      // ChatPage.statusStackLayering.test.tsx pins the ordering.
      className="relative z-[2] mx-auto w-full px-4"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
      data-testid="subagent-delivery-progress"
    >
      {/* The dock's glass (components/Glass.tsx) on the accent tint step, like
          the sub-agent bar this line stands in for once the wave has landed. */}
      <Glass variant="chip" radius={8} className="mb-1 flex items-center gap-2 glass-accent px-3 py-1.5 text-[12px] font-mono text-muted">
        <Bot size={13} className="text-accent/70 shrink-0" />
        <Loader2 size={12} className="animate-spin text-accent/70 shrink-0" />
        <span>
          {i18nT('components.queueStack.sub_agent_result', { count: count })} {i18nT('components.queueStack.ready_processing_after_the_current_turn')}
        </span>
      </Glass>
    </div>
  )
}

const MAX_PEEK = 2
const CARD_H = 40
const PEEK = 6
const EXPANDED_GAP = 4
const SCALE_STEP = 0.04
const HIDDEN_EXTRA_SCALE = 0.02
const OVERLAP = 11 // overlap to fuse with input area below

const SPRING = { type: 'spring' as const, stiffness: 400, damping: 30 }

/** Inline editor (textarea + save) swapped in for the message text while editing.
 *  Owns the live value so its own controls commit the typed text, never stale content.
 *
 *  A textarea, not an `<input>`: a queued message can span several lines --
 *  the attachment serializer writes one `[attached_file N] path` marker per
 *  line -- and a single-line input drops every newline from its value, so an
 *  ordinary edit would glue the markers together and the queue edit's
 *  whitespace-bounded marker match would prune every attachment but the last.
 *  Enter commits (the composer's own contract); Shift+Enter inserts a line. */
function EditInput({ initial, onCommit, onCancel, onDraftChange, pastes = [] }: {
  initial: string
  /** The edited text and the blocks still LIVE in it (`liveBlocks`: the
   *  opening blocks whose id-carrying token stands whole in the text), so
   *  the commit expands exactly the tokens the editor shows: not one whose
   *  token was deleted, cut or expanded inline, and every one a native undo
   *  put back. */
  onCommit: (value: string, blocks: PasteBlock[]) => void
  onCancel: () => void
  onDraftChange?: (value: string | undefined, blocks: PasteBlock[]) => void
  /** The blocks behind the `[ Paste #N ]` tokens the value holds when the
   *  editor opens. A token is one unit here, through the composer's own
   *  `usePasteTokens`: Backspace or Delete beside it removes it whole, the
   *  caret and a selection step over it, a click selects it whole, a double
   *  click expands it inline (and retires its block). A value that holds a
   *  token the edit broke anyway (`hasBrokenToken`) is not committed, because
   *  the commit expands only whole tokens and the agent would receive the
   *  broken text in place of the paste. */
  pastes?: PasteBlock[]
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  const [value, setValue] = useState(initial)
  // The blocks still live in the value, derived from the value itself
  // (`liveBlocks`): a token deleted, cut or expanded inline drops out, and a
  // token a native undo puts back comes back with it.
  const blocks = liveBlocks(value, pastes)
  const blocksRef = useRef(blocks)
  blocksRef.current = blocks
  // Guard so blur and an explicit save/Enter don't both fire onCommit.
  const committedRef = useRef(false)
  // Select the FIRST line only, never the whole value: the marker lines sit
  // below the single visible row, and a select-all would let an ordinary
  // retype replace them unseen -- the queue edit then prunes every
  // attachment from the send with nothing on screen to say so.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    const nl = initial.indexOf('\n')
    el.setSelectionRange(0, nl === -1 ? initial.length : nl)
  }, [initial])
  const currentDraftRef = useRef(initial)
  currentDraftRef.current = value
  // A close can unmount this local state before blur or Save commits it. Keep
  // the side surface's optional owner current, and retire the copy on every exit.
  useEffect(() => {
    onDraftChange?.(currentDraftRef.current, blocksRef.current)
    return () => onDraftChange?.(undefined, blocksRef.current)
  }, [initial, onDraftChange])
  // Lines below the visible one, surfaced as a count so the hidden part of
  // the value is never a surprise. When every hidden line is an attachment
  // marker (the serializer's `[attached_file N] path` / `[attached_dir N]
  // path` lines) the cue names them as attachments -- "+2 attachments" says
  // what is there, where "+2 lines" only says how much.
  const hidden = value.split('\n').slice(1)
  const hiddenLines = hidden.length
  const hiddenAreAttachments = hiddenLines > 0 && hidden.every(l => /^\[attached_(?:file|dir) \d+\] /.test(l))
  const hiddenCue = hiddenAreAttachments
    ? i18nT('components.queueStack.hidden_attachments', { count: hiddenLines })
    : i18nT('components.queueStack.hidden_lines', { count: hiddenLines })
  // Commit only a real change: skip empty and unchanged values so a stray
  // focus→blur (or clear→blur) doesn't fire a no-op PATCH + WS broadcast.
  const commit = () => {
    if (committedRef.current) return
    // A broken token stays in the editor for the user to finish removing or
    // put back; nothing is sent and nothing is lost. Focus is left where the
    // user put it: a blur that pulled focus back would trap the pointer.
    if (hasBrokenToken(value, initial, pastes)) return
    committedRef.current = true
    const trimmed = value.trim()
    if (trimmed && trimmed !== initial.trim()) onCommit(value, blocks)
    else onCancel()
  }
  const cancel = () => { if (committedRef.current) return; committedRef.current = true; onCancel() }
  // The composer's own paste-token editing (`usePasteTokens`): a token deletes
  // as one unit, the caret and a selection step over it, a click selects it
  // whole, and copy or cut puts the pasted text on the clipboard, not the
  // token. One rule, one spelling; the hook is store-free and both composers
  // bind their textarea to it. Paste is NOT bound: a paste collapsed here
  // would mint a block the row does not carry, and the commit expands only
  // the blocks the editor still holds.
  const valueFromUserRef = useRef(false)
  const { handleTokenKey, handleSelectSnap, handleCopy, handleCut, handleTextareaClick } = usePasteTokens({
    value,
    onChange: v => { setValue(v); currentDraftRef.current = v; onDraftChange?.(v, liveBlocks(v, pastes)) },
    pasteBlocks: blocks,
    // The list is derived from the value above, so the hook's own pruning
    // has nothing to write; the callback is present so a double click may
    // expand a token inline (the hook requires it for that).
    onPasteBlocksChange: () => {},
    showFullPastes: false,
    inputRef: ref,
    valueRef: currentDraftRef,
    valueFromUserRef,
    recordCaret: () => {},
    ime,
  })
  return (
    <>
      <textarea
        ref={ref}
        value={value}
        // One visible row: the card is a fixed-height stack slot (CARD_H) and
        // shows the content itself truncated to one line, so the editor shows
        // the same line the card does. The value keeps every newline; the
        // textarea scrolls to the caret as the user moves through the lines.
        rows={1}
        onChange={e => {
          setValue(e.target.value)
          currentDraftRef.current = e.target.value
          onDraftChange?.(e.target.value, liveBlocks(e.target.value, pastes))
        }}
        // Stop the card's expand/collapse + drag handlers from swallowing pointer + key events.
        onPointerDown={e => e.stopPropagation()}
        onClick={e => { e.stopPropagation(); handleTextareaClick(e) }}
        onSelect={handleSelectSnap}
        onCopy={handleCopy}
        onCut={handleCut}
        onKeyDown={e => {
          e.stopPropagation()
          if (handleTokenKey(e)) return
          if (e.key === 'Enter' && !e.shiftKey) {
            // The commit's own emptiness check stays in commit(). claimEnter
            // consumes the keypress, so a committing Enter never inserts a line.
            if (ime.claimEnter(e)) commit()
          } else if (e.key === 'Escape') { e.preventDefault(); ime.reset(); cancel() }
        }}
        {...ime.bindComposition({ onBlur: commit })}
        className="flex-1 min-w-0 resize-none overflow-hidden bg-[var(--bg)] text-[var(--text)] placeholder:text-[var(--muted)] rounded px-1.5 py-0.5 text-[13px] leading-5 outline-hidden border border-[var(--border)] focus-visible:border-[var(--accent)]"
        aria-label={i18nT('components.queueStack.edit_queued_message')}
      />
      {hiddenLines > 0 && (
        <span className="shrink-0 text-[11px] text-[var(--muted)] tabular-nums" data-testid="queue-edit-hidden-lines"
          title={hiddenCue}>
          {hiddenCue}
        </span>
      )}
      <button className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors text-[var(--text)]"
        title={i18nT('components.queueStack.save')} aria-label={i18nT('components.queueStack.save_edit')}
        // mousedown commits before the input's blur can fire with the same value.
        onMouseDown={e => { e.preventDefault(); e.stopPropagation() }}
        onClick={e => { e.stopPropagation(); commit() }}>
        <Check size={13} />
      </button>
    </>
  )
}

function QueueStackInner({ messages, onCancel, onInterrupt, onEdit, onEditDraft, onReorder, fuseBelow = true, pendingIds }: {
  messages: ChatMessage[]
  onCancel?: (queueId: string) => void
  onInterrupt?: (queueId: string) => void
  /** `pastes` is set when the entry carried paste blocks: the ones whose
   *  tokens the edit kept, in token order (empty when it kept none), for the
   *  row to carry on. Absent when the entry had none to begin with. */
  onEdit?: (queueId: string, content: string, pastes?: PasteBlock[]) => void
  onEditDraft?: (queueId: string, content: string | undefined) => void
  /** Move a queued message one step toward the front (`next`) or the back
   *  (`later`) of the run order. Index 0 runs first. */
  onReorder?: (queueId: string, direction: 'next' | 'later') => void
  /** Queue ids whose cancel/edit is in flight. Their controls are disabled so a
   *  second click cannot fire a duplicate request — on a surface where the card
   *  is only retired once the server confirms, that second request races the
   *  first and comes back 404, reporting a failure for an action that worked. */
  pendingIds?: ReadonlySet<string>
  /** When true (default) the front collapsed card fuses into the surface directly
   *  below it (the input box) via a negative bottom margin + a flat, borderless bottom
   *  edge. Set false when a non-fusable element sits between the queue and the input box
   *  (follow-up option chips or the knowledge chip): the card then keeps its negative
   *  margin off and renders as a complete rounded card cleanly above that element instead
   *  of overlapping it. */
  fuseBelow?: boolean
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [_expanded, setExpanded] = useState(false)
  const expanded = _expanded && messages.length > 1
  // A stack handed no action callbacks is a read-only view (ChatEmbed shows
  // the queue this way). Its cards say so in a tooltip, and a single card --
  // which has nothing to expand either -- drops the pointer cursor, so a reader
  // does not hesitate over a click that would do nothing (#19920). The
  // dashboard always passes callbacks, so its stack is unchanged.
  const readOnly = !onCancel && !onInterrupt && !onEdit && !onReorder
  const clickable = messages.length > 1 || !readOnly
  const readOnlyHint = readOnly ? i18nT('components.queueStack.read_only_waiting_to_send') : undefined
  const [editingId, setEditingId] = useState<string | null>(null)
  // The row as it was when its editor opened. The editor's value, the blocks
  // behind its tokens, the change check, the expansion on commit and the
  // record of the kept blocks all read this one snapshot: a `queue_edit`
  // frame from another tab rewrites the live row under an open editor, and
  // an editor holding a token while the live row had lost its blocks would
  // commit the literal token in place of the paste.
  const editingRow = useRef<ChatMessage | null>(null)

  // Reset expanded when queue drains to trivial size
  useEffect(() => {
    if (messages.length <= 1) setExpanded(false)
  }, [messages.length])

  // Drop a stale edit target if its card leaves the queue (e.g. dequeued / cancelled).
  useEffect(() => {
    if (editingId && !messages.some(m => (m.meta?.queueId as string) === editingId)) setEditingId(null)
  }, [messages, editingId])

  const commitEdit = (queueId: string, content: string, pastes?: PasteBlock[]) => {
    setEditingId(null)
    if (!onEdit) return
    if (pastes) onEdit(queueId, content, pastes)
    else onEdit(queueId, content)
  }
  const cancelEdit = () => setEditingId(null)

  const peekCount = Math.min(MAX_PEEK, Math.max(0, messages.length - 1))
  const collapsedHeight = messages.length > 0 ? CARD_H + peekCount * PEEK : 0
  const expandedHeight = messages.length > 0 ? messages.length * CARD_H + (messages.length - 1) * EXPANDED_GAP : 0

  const targetHeight = expanded ? expandedHeight : collapsedHeight
  const targetMargin = messages.length > 0 && !expanded && fuseBelow ? -OVERLAP : 0

  // Imperatively control margin: spring on expand/collapse, snap on enter/exit
  const marginMV = useMotionValue(targetMargin)
  const marginSpring = useSpring(marginMV, SPRING)
  const prevExpanded = useRef(expanded)

  useEffect(() => {
    const expandChanged = prevExpanded.current !== expanded
    prevExpanded.current = expanded

    if (expandChanged) {
      // Expand/collapse: animate via spring
      marginMV.set(targetMargin)
    } else if (messages.length > 0) {
      // Enter (count increased) or count decreased but not to 0: snap immediately
      // When count hits 0, let onExitComplete handle the margin reset
      marginSpring.jump(targetMargin)
    }
  }, [expanded, targetMargin, messages.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Handle last-card exit: snap margin to 0 when AnimatePresence finishes
  const prevCountForExit = useRef(messages.length)
  const hasExitingRef = useRef(false)
  useEffect(() => {
    if (messages.length < prevCountForExit.current) hasExitingRef.current = true
    prevCountForExit.current = messages.length
  }, [messages.length])

  const onExitComplete = () => {
    hasExitingRef.current = false
    if (messages.length === 0) marginSpring.jump(0)
  }

  return (
    // `zIndex: 2`: an explicit layer in the composer dock's status stack, far
    // below the composer's own `z-10`, so the collapsed card's -OVERLAP fuse
    // keeps sliding UNDER the input box rather than over it.
    // ChatPage.statusStackLayering.test.tsx pins the ordering.
    <div className="px-4 mx-auto w-full relative" style={{ maxWidth: 'var(--mc-content-width, 900px)', zIndex: 2 }}>
      {/* The tooltip is pointer-only, so a screen reader gets the same words
          once, as text BESIDE the stack: inside it they would join the
          expand button's accessible name once per card. */}
      {readOnlyHint && messages.length > 0 && <span className="sr-only" data-testid="queue-readonly-hint">{readOnlyHint}</span>}
      <motion.div
        className={clickable ? 'relative cursor-pointer' : 'relative'}
        animate={{ height: targetHeight }}
        transition={SPRING}
        style={{ marginBottom: marginSpring }}
        onClick={() => messages.length > 1 && setExpanded(e => !e)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if ((e.key === 'Enter' || e.key === ' ') && messages.length > 1) {
            e.preventDefault()
            setExpanded(prev => !prev)
          }
        }}
        role={messages.length > 1 ? 'button' : undefined}
        tabIndex={messages.length > 1 ? 0 : undefined}
        aria-expanded={messages.length > 1 ? expanded : undefined}
      >
        <AnimatePresence initial={false} onExitComplete={onExitComplete}>
          {messages.map((m, i) => {
            let y: number
            let scale: number
            let opacity: number
            let zIndex: number

            if (expanded) {
              const pos = messages.length - 1 - i
              y = pos * (CARD_H + EXPANDED_GAP)
              scale = 1
              opacity = 1
              zIndex = pos + 1
            } else if (i <= MAX_PEEK) {
              const depth = i
              y = (collapsedHeight - CARD_H) - depth * PEEK
              scale = 1 - (depth + 1) * SCALE_STEP
              opacity = 1
              zIndex = (MAX_PEEK + 1) - depth
            } else {
              y = (collapsedHeight - CARD_H) - MAX_PEEK * PEEK
              scale = 1 - (MAX_PEEK + 1) * SCALE_STEP - HIDDEN_EXTRA_SCALE
              opacity = 0
              zIndex = 0
            }

            const isFrontCollapsed = !expanded && i === 0
            const queueId = m.meta?.queueId as string | undefined
            const isEditing = !!queueId && editingId === queueId
            const isPending = !!queueId && !!pendingIds?.has(queueId)
            // Per-card actions show on the front single card or when expanded.
            const showActions = (expanded || messages.length === 1) && !!queueId
            // App-message entries are read-only-but-visible: the backend
            // refuses queue_edit_by_id for system-injection kinds (the user's
            // replacement words would drain app-authored), so the card must
            // not offer the pencil. Cancel/interrupt/reorder stay: they change
            // WHEN or WHETHER the entry runs, never who authored its text.
            const isAppEntry = isAppMessageQueued(m)
            const displayText = queuedDisplayText(m)
            // The card hides the knowledge envelope, so this chip is what says
            // the send still carries it. Same icon as the sent bubble's
            // knowledge chip, so the two read as one thing.
            const knowledgeChip = queuedHasKnowledge(m) ? (
              <span
                data-testid="queue-card-knowledge"
                // A pill, not a bare glyph: beside the pencil / zap / X buttons a
                // bare icon reads as a fourth button. `min-w-0` + the truncating
                // label let the chip give way before the preview does.
                className="min-w-0 inline-flex items-center gap-1 rounded-full border border-current px-1.5 text-[11px] leading-4 opacity-80"
                title={i18nT('components.queueStack.knowledge_attached_title')}
              >
                <BookOpen size={11} className="shrink-0" aria-hidden="true" />
                {/* The label shows at the usual pane width. Only on a really
                    narrow card (a phone, a three-way split) does the chip drop
                    to the icon, so it cannot squeeze the preview beside up to
                    five action buttons. The label stays in the accessibility
                    tree there, and the title keeps the meaning. */}
                <span data-testid="queue-card-knowledge-label" className="truncate @max-[360px]/queuecard:sr-only">
                  {i18nT('components.queueStack.knowledge_attached_label')}
                </span>
              </span>
            ) : null

            return (
              // The motion box only places the card (peek offset, scale, layer);
              // the card itself is the composer dock's glass on the warn tint
              // step. Two things the old solid card animated are gone with it:
              // the per-depth `brightness()` filter (a filter on the box would
              // make it the backdrop root, and the glass inside would have
              // nothing left to blur) and the square-bottomed "fused" corners
              // (the primitive has one radius; the front card's bottom -OVERLAP
              // now sits UNDER the composer's own glass, which is the seam).
              <motion.div
                key={m.meta?.queueId as string ?? m.ts ?? `q-${i}-${m.content}`}
                initial={false}
                animate={{ opacity, y, scale }}
                exit={{ y: y + 40, zIndex: 50, transition: SPRING }}
                transition={SPRING}
                className="absolute top-0 left-0 right-0"
                style={{ transformOrigin: 'bottom center', height: CARD_H, zIndex }}
              >
              {/* `glass-warn`, not a theme-specific color: the warn tint step is
                  what every pending-decision pane in the dock wears, and the
                  solid fallbacks in index.css mix the same hue into
                  `--bg-elevated` where the glass cannot paint. Cards behind
                  peek out above this one as more glass, as a stack of panes
                  would. */}
              {/* `data-testid="queue-card"` is the hook the capture harnesses
                  (capture-queued-cancel-restore, capture-members-steer-only)
                  wait on; it replaces the old `queue-card` class, which no
                  longer has a style to carry. */}
              <Glass
                variant="chip"
                radius={12}
                data-testid="queue-card"
                // `glass-warn-fg` (index.css): `--warn` in dark palettes, a
                // darker mix of it in light ones, where plain `--warn` on this
                // tint fails the 4.5:1 text floor (#19920).
                // `@container/queuecard`: the knowledge chip reads THIS card's
                // width, never the viewport, since a split pane is narrow on
                // a wide screen.
                className="@container/queuecard h-full glass-warn px-3 py-2 text-[13px] glass-warn-fg"
                title={readOnlyHint}
              >
                <span className="flex items-center gap-1.5 h-full">
                  <span className="shrink-0 text-[10px] font-mono opacity-50 w-4 text-center">{i + 1}</span>
                  {isFrontCollapsed && (
                    <span className="shrink-0 inline-flex animate-[hourglass-flip_3s_ease-in-out_infinite]">
                      <Hourglass size={13} />
                    </span>
                  )}
                  {isEditing && onEdit ? (
                    <>
                      <EditInput
                        initial={queuedOwnText(editingRow.current ?? m)}
                        pastes={splitQueuedContent(editingRow.current ?? m).pastes}
                        onCommit={(v, blocks) => {
                          const opened = editingRow.current ?? m
                          const wire = reglueQuote(opened, v, blocks)
                          const live = recordEditedPastes(opened, v, wire, blocks)
                          commitEdit(queueId!, wire, queuedPastes(opened).length ? live : undefined)
                        }}
                        onCancel={cancelEdit}
                        // Blank typed words report no draft, checked before the
                        // reglue: the card's quote or knowledge envelope would
                        // otherwise make a cleared editor read as an edit.
                        onDraftChange={onEditDraft
                          ? (value, blocks) => onEditDraft(queueId!, value === undefined || !value.trim() ? undefined : reglueQuote(editingRow.current ?? m, value, blocks))
                          : undefined}
                      />
                      {/* Kept beside the edit box: the editor holds only the
                          typed words, and the chip says the knowledge stays
                          with the send after the save. */}
                      {knowledgeChip}
                    </>
                  ) : (
                    <>
                      {/* Same attribution the transcript row wears: a queued
                          app message must not read as the user's own words
                          while it waits (one message, one identity). The
                          visible "Queued" word states the pending state in
                          words — the hourglass alone is icon-only, and color
                          coincidence is not how a reader should have to link
                          the strip to the transcript row it becomes. */}
                      {isAppEntry && (
                        <span className="min-w-0 shrink text-muted text-[11px] inline-flex items-center gap-1 cursor-help" title={i18nT('components.mcpApp.from_app_tooltip')}>
                          <span className="shrink-0 uppercase tracking-wide opacity-70">{i18nT('components.queueStack.queued')}</span>
                          <AppWindow size={11} className="shrink-0" />
                          <span className="truncate">{i18nT('components.mcpApp.from_app', { app: String((m.meta?.appLabel as string) || 'app').split('/')[0] })}</span>
                        </span>
                      )}
                      {/* `min-w-16` only when a chip shares the row: the chip
                          gives way (truncates) before the typed words do. */}
                      <span className={`truncate flex-1${knowledgeChip ? ' min-w-16' : ''}`}>{displayText}</span>
                      {knowledgeChip}

                      {/* Reorder arrows only make sense with 2+ cards, and only
                          in the expanded stack where the run order is visible.
                          Index 0 runs first and renders at the BOTTOM of the
                          expanded stack, so "run sooner" moves the card DOWN
                          visually: ArrowDown = sooner, ArrowUp = later. */}
                      {onReorder && expanded && messages.length > 1 && (
                        <>
                          <button
                            className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
                            title={i18nT('components.queueStack.run_sooner')}
                            aria-label={i18nT('components.queueStack.run_sooner')}
                            disabled={i === 0}
                            onClick={(e) => { e.stopPropagation(); onReorder(queueId!, 'next') }}
                          >
                            <ArrowDown size={13} />
                          </button>
                          <button
                            className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
                            title={i18nT('components.queueStack.run_later')}
                            aria-label={i18nT('components.queueStack.run_later')}
                            disabled={i === messages.length - 1}
                            onClick={(e) => { e.stopPropagation(); onReorder(queueId!, 'later') }}
                          >
                            <ArrowUp size={13} />
                          </button>
                        </>
                      )}
                      {onEdit && showActions && !isAppEntry && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT('components.queueStack.edit_queued_message')}
                          aria-label={i18nT('components.queueStack.edit_queued_message')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); editingRow.current = m; setEditingId(queueId!) }}
                        >
                          <Pencil size={13} />
                        </button>
                      )}
                      {onInterrupt && showActions && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors text-[var(--text)] disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT('components.queueStack.interrupt_current_turn_and_send_this_now')}
                          aria-label={i18nT('components.queueStack.send_now')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); onInterrupt(queueId!) }}
                        >
                          <Zap size={13} fill="currentColor" />
                        </button>
                      )}
                      {onCancel && showActions && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT(isAppEntry ? 'components.queueStack.cancel_queued_message' : 'components.queueStack.cancel_and_move_back_to_input')}
                          aria-label={i18nT('components.queueStack.cancel_queued_message')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); onCancel(queueId!) }}
                        >
                          <X size={13} />
                        </button>
                      )}
                      {isFrontCollapsed && messages.length > 1 && (
                        <span className="shrink-0 flex items-center gap-1 text-[11px] opacity-70">
                          {messages.length} {i18nT('components.queueStack.queued')}
                          <ChevronUp size={12} />
                        </span>
                      )}
                      {expanded && i === 0 && (
                        <ChevronUp size={13} className="shrink-0 opacity-50 rotate-180" />
                      )}
                    </>
                  )}
                </span>
              </Glass>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

export default memo(QueueStackInner, (prev, next) =>
  prev.messages.length === next.messages.length &&
  prev.fuseBelow === next.fuseBelow &&
  prev.pendingIds === next.pendingIds &&
  prev.messages.every((m, i) => m === next.messages[i]) &&
  prev.onCancel === next.onCancel &&
  prev.onInterrupt === next.onInterrupt &&
  prev.onEdit === next.onEdit &&
  prev.onEditDraft === next.onEditDraft &&
  prev.onReorder === next.onReorder
)
