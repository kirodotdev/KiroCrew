import { useMemo, useState } from 'react'
import type React from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, ChevronDown, ChevronRight, Folder, Paperclip, Plug } from 'lucide-react'

import { api } from '../../api/client'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { revealOrOpen, useRevealFailure } from '../../components/FilePathMenu'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import PastedChip from '../../components/PastedChip'
import SessionActionsMenu from '../../components/SessionActionsMenu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { deriveLoadedMcpTools } from '../../lib/mcpLoadedTools'
import { useAppSelector } from '../../store'
import type { ChatMessage, McpServer } from '../../types'
import {
  buildFileLabels,
  findUnreferencedAttachments,
  parseDirs,
  parseFiles,
  resolveDirSegment,
  resolveFileSegment,
} from '../../utils/fileTokens'
import { findTokenRanges, recollapsePastes, type PasteBlock } from '../../utils/pasteTokens'
import { TURN_OPENER_ROLES } from './groupDisplayItems'
import McpToolsPanel from './McpToolsPanel'
import type { DisplayItem, TurnItem } from './types'

export function ChatHeaderMenu({ activeSlot, agent, onReveal, onRename, mode }: {
  activeSlot: string | null; agent?: string; onReveal?: () => void; onRename?: () => void; mode?: string
}) {
  // Controlled open state: lets the colour-swatch row (not a Radix menu item)
  // close the menu after a pick, via the onColorPicked hook passed below.
  const [open, setOpen] = useState(false)
  // MCP server list is fetched lazily when its submenu opens (driven by the
  // Radix Sub's open state).
  const [mcpOpen, setMcpOpen] = useState(false)
  const { data: servers = [] } = useQuery<{ name: string; enabled?: boolean }[]>({
    queryKey: ['mcp-servers', agent],
    queryFn: () => api.mcpActive(agent || undefined),
    enabled: mcpOpen,
  })
  // Tool Search mode for this session's MCP tools (shared ['kirocrewConfig']
  // cache). When on, tool specs are deferred (search-and-call), so every server
  // shows as connected but its tools load only when used; when off, every spec
  // is sent each turn. Explains the "why are they all loaded?" question.
  const { data: toolSearchOn = true } = useQuery<{ agent?: { tool_search?: boolean } }, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c.agent?.tool_search ?? true,
    enabled: mcpOpen,
  })
  // Per-tool loaded/deferred state is derived client-side (no endpoint): the
  // full server list carries each server's tool names + disabledTools, and the
  // "loaded this session" set comes from scanning this slot's tool_search
  // results in the chat store. See deriveLoadedMcpTools for the caveats.
  const { data: fullServers = [] } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers-full'],
    queryFn: () => api.mcpServers(),
    enabled: mcpOpen,
  })
  const toolsByServer = useMemo(
    () => Object.fromEntries(fullServers.map(s => [s.name, { tools: s.tools, disabledTools: s.disabledTools }])),
    [fullServers],
  )
  const sessionMessages = useAppSelector(s => s.chat.messages)
  const loadedTools = useMemo(() => deriveLoadedMcpTools(sessionMessages), [sessionMessages])
  // What this slot's session actually reported about its MCP servers. Pushed by
  // the gateway onto the same `slots` array the snapshot rehydrates, so it needs
  // no query of its own — and unlike the ['mcp-servers'] query above it is keyed
  // by SLOT, which is the whole point: that query answers a question about the
  // agent's configuration and this answers one about this session.
  const sessionReport = useAppSelector(
    s => (activeSlot ? s.dashboard.slots?.find(sl => sl.key === activeSlot)?.mcp_report : null) ?? null,
  )

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button className="px-0.5 py-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none transition-all" aria-label={i18nT('pages.chatPage.session_options')}>
          <ChevronDown size={14} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[180px]">
        {activeSlot && (
        <SessionActionsMenu
          variant="dropdown"
          slotKey={activeSlot}
          mode={mode}
          // MCP servers: stateful (lazy fetch gated on the sub's open state), so
          // it stays here as an info slot rather than a generic capability.
          infoSlots={[
            <DropdownMenuSub key="mcp" onOpenChange={setMcpOpen}>
              <DropdownMenuSubTrigger>
                <Plug size={13} className="shrink-0 text-muted" />
                <span className="flex-1">{i18nT('pages.chatPage.mcp_servers')}</span>
                <ChevronRight size={12} className="text-muted" />
              </DropdownMenuSubTrigger>
              {/* Intentional tighter cap composed via min() with the primitive's
                  available-height var: 340px keeps the MCP tools submenu compact
                  while preserving the viewport never-clip floor (a bare max-h
                  would override the primitive, since cn()'s tailwind-merge dedupes
                  max-h-*). overflow is left to the primitive. */}
              <DropdownMenuSubContent className="min-w-[240px] max-w-[300px] max-h-[min(340px,var(--radix-dropdown-menu-content-available-height))] px-3 py-2">
                <McpToolsPanel
                  servers={servers}
                  toolsByServer={toolsByServer}
                  loaded={loadedTools}
                  toolSearchOn={toolSearchOn}
                  loading={servers.length === 0}
                  sessionReport={sessionReport}
                />
              </DropdownMenuSubContent>
            </DropdownMenuSub>,
          ]}
          onReveal={onReveal}
          onRename={onRename}
          // The header controls its own menu, so close it after a colour pick.
          onColorPicked={() => setOpen(false)}
        />
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Per-message identity key with row-id tie-break. `msgKey` alone is NOT
 *  unique — a coarse OS clock can stamp two rows appended in one tick with the
 *  same `ts` (see isRedeliveredMessage in chatSlice on why row identity is
 *  `meta.mid`, not a ts tuple). `mid` is stamped once per row and survives
 *  every delivery door (HTTP rebuild, WS broadcast, JSONL round trip), so the
 *  suffix is as reload-stable as the key it disambiguates. Rows without a
 *  `mid` (locally-minted streaming/optimistic bubbles) fall back to `msgKey`
 *  alone, which is exactly the uniqueness they had before. */
/** Client-generated one-shot correlation id for an optimistic user bubble.
 *  The server preserves meta fields on the user row it appends, so an echo or
 *  transcript page carries this id back and the bubble is matchable without
 *  relying on content equality (#2845). Shared by the plain send path and the
 *  mid-turn steer path (#6075) so the two cannot drift in id shape. */
export function mintSendId(): string {
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export function msgIdentityKey(m: ChatMessage, msgKey: (m: ChatMessage) => string): string {
  const mid = m.meta?.mid
  return typeof mid === 'string' && mid ? `${msgKey(m)}~${mid}` : msgKey(m)
}

/** Stable key for a single TurnItem — the leading row of a turn OR a top-level
 *  single/group. A `single` and the `turn` it leads resolve to the SAME key so
 *  a mid-stream regroup (single promoted into a grouped turn once it gains
 *  working steps) does NOT change the row's virtual key → no remount / silent
 *  re-measure. `msgKey` supplies the per-message identity (clientTs → ts →
 *  minted id; never the array index — see stableMsgKey). Groups key on their
 *  FIRST MESSAGE's identity, never `startIdx`: a prepend (history backfill)
 *  renumbers every array index but leaves message identities intact, so a
 *  group-led row keeps its key — and with it its cached height, DOM node, and
 *  scroll anchor — across the shift. The index key this replaces was unique by
 *  construction, so group keys go through `msgIdentityKey` to keep that
 *  property across same-tick `ts` ties.
 *
 *  `msgs` is non-empty by construction (both producers emit a group only under
 *  `if (group.length)`), but the type allows `[]` and this is a public export —
 *  degrade to the index rather than throwing inside `msgKey`. */
export function turnLeadKey(it: TurnItem, msgKey: (m: ChatMessage) => string): string {
  if (it.kind === 'single') return `row-${msgKey(it.msg)}`
  const lead = it.msgs[0]
  return lead ? `grp-${msgIdentityKey(lead, msgKey)}` : `grp-idx-${it.startIdx}`
}

/** Virtualizer / HeightCache key for a display row. Pure (identity injected)
 *  so the steer-reconcile-stability and regroup-stability guarantees are
 *  unit-testable. A `turn` inherits the key of its leading item so promoting a
 *  single into a turn (and vice-versa) keeps the row identity — and thus its
 *  cached height and DOM node — stable. */
/** Anchor identity that survives a prepend's key reshuffle: the TAIL message's
 *  identity. A page landing regroups older messages into the top turn's HEAD —
 *  renaming its lead-derived display key — but a turn's newest message is
 *  untouched by content arriving before it, so an anchor held by the tail
 *  resolves across the landing and its compensation is not dropped. Falls back
 *  to positional markers only for degenerate empty rows, mirroring
 *  virtualKeyFor's own fallbacks. */
/** SECOND anchor identity for a display row: its LEAD message, `l-` prefixed so
 *  it shares no vocabulary with a tail id and the two can never cross-match.
 *
 *  Exists because neither end of a turn is stable on its own. `stableAnchorIdFor`
 *  takes the tail, which a page landing cannot rename -- but a turn STILL
 *  STREAMING gains messages at that end, so its tail id changes under a reader
 *  who has not moved. The lead is untouched by appends (and by a single being
 *  promoted into the turn it leads, which keeps the first message). Persisting
 *  both lets the restore match whichever end survived. */
export function anchorAltIdFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
): string {
  const leadOf = (t: TurnItem): ChatMessage | null =>
    t.kind === 'single' ? t.msg : (t.msgs[0] ?? null)
  let lead: ChatMessage | null = null
  if (it.kind === 'turn') {
    const first = it.items[0]
    lead = first ? leadOf(first) : null
  } else {
    lead = leadOf(it)
  }
  if (!lead) return `alt-empty-${index}`
  return `l-${msgIdentityKey(lead, msgKey)}`
}

export function stableAnchorIdFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
): string {
  const tailOf = (t: TurnItem): ChatMessage | null =>
    t.kind === 'single' ? t.msg : (t.msgs[t.msgs.length - 1] ?? null)
  let tail: ChatMessage | null = null
  if (it.kind === 'turn') {
    const last = it.items[it.items.length - 1]
    tail = last ? tailOf(last) : null
  } else {
    tail = tailOf(it)
  }
  if (!tail) return `anchor-empty-${index}`
  return `a-${msgIdentityKey(tail, msgKey)}`
}

export function virtualKeyFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
  isTrailing = false,
): string {
  if (it.kind === 'turn') {
    const first = it.items[0]
    if (!first) return `turn-empty-${index}`
    // A turn WITH its opening prompt keys on that lead: the lead never
    // changes once the prompt is loaded, and the trailing turn's tail grows
    // every stream tick (tail-keying it would remount per token).
    //
    // A HEADLESS turn -- the topmost boundary turn of a partially loaded
    // transcript, whose opening prompt is still in an unloaded older page --
    // keys on its TAIL instead. Every older-page landing feeds that turn's
    // HEAD, so a lead-derived key renamed the row per landing: React sees a
    // new element, unmounts the giant row and remounts it (Pierre surfaces
    // visibly "reload", and the height cache line is orphaned) -- once per
    // walk wave, which is the refresh-then-walk bounce. The tail is
    // untouched by content arriving above it, so the key holds across the
    // whole walk; when the opening prompt finally lands the key flips to
    // the lead ONCE (one remount, its height migrated by the departure
    // rename pass). A headless turn is by construction not the trailing
    // streaming turn in any live session older than one page, and a fresh
    // session's single turn has its prompt loaded, so tail growth cannot
    // re-key it in practice.
    // ...with one exception: the TRAILING turn is never tail-keyed, even
    // headless. A refresh into a giant in-flight turn loads a window that
    // is entirely that one turn -- headless AND growing at the tail, where
    // a tail key would remount it per stream tick. Lead-keying it merely
    // keeps the pre-fix behavior (renamed per landing) for the one turn
    // that is anchored to the viewport bottom anyway.
    // ...and only at INDEX 0: the walk feeds the head of the TOPMOST row
    // alone. A mid-list turn without an opener lead (an interim fold, a
    // single mid-stream promoting into the turn it leads) keeps the #253
    // lead-key contract -- its head is bounded by settled content, so
    // landings cannot rename it, and tail-keying it would itself remount
    // the row at every fold boundary.
    const leadMsg = first.kind === 'single' ? first.msg : first.msgs[0]
    // `complete === false` is the running-turn marker (applyRunningState):
    // an in-flight turn grows at its tail and must never key on it,
    // whatever its position -- the semantic twin of the positional
    // isTrailing guard, and the one that holds when the loaded window IS
    // one giant in-flight turn (refresh mid-turn: index 0 AND trailing).
    if (index === 0 && !isTrailing && it.complete !== false && leadMsg && !TURN_OPENER_ROLES.has(leadMsg.role)) {
      const last = it.items[it.items.length - 1]
      const tail = last
        ? (last.kind === 'single' ? last.msg : (last.msgs[last.msgs.length - 1] ?? null))
        : null
      if (tail) return `hlt-${msgIdentityKey(tail, msgKey)}`
    }
    return turnLeadKey(first, msgKey)
  }
  return turnLeadKey(it, msgKey)
}

/** Virtualizer keys for the WHOLE display list, with a collision tie-break.
 *
 *  `virtualKeyFor` is not unique across the list: a `single` keys on
 *  `msgKey` alone (`row-<ts>`), and a coarse OS clock can stamp two rows
 *  appended in one tick with the same `ts` — the exact hazard `msgIdentityKey`
 *  closes for group leads. Two rows sharing one key reach React as duplicate
 *  siblings (one is silently dropped from the DOM — content visibly missing)
 *  and share one HeightCache slot (each re-measure of either row reprices the
 *  other, oscillating the spacers). Same failure from an overlapping older
 *  page whose rows lack the `meta.mid` the prepend dedup keys on.
 *
 *  The tie-break is positional among COLLIDERS ONLY: the first occurrence
 *  keeps the bare key — so the common case is byte-identical to
 *  `virtualKeyFor` and every cached height, DOM node, and scroll anchor keyed
 *  before this pass survives — and each later duplicate gets an occurrence
 *  suffix. Deterministic for a given list order, so keys are stable across
 *  re-renders. An insert BEFORE a collider shifts which physical row holds
 *  the bare key: those rows remount (a `~#N` height or scroll anchor
 *  persisted under the old occupant can also go stale until re-measured) —
 *  bounded to rows that previously rendered broken (dropped sibling), and
 *  strictly better than that render.
 *
 *  NOT folded into `virtualKeyFor`: uniqueness is a property of the list, not
 *  of one row, and a per-row `~mid` suffix instead would rename every
 *  streamed/optimistic row (which lacks `mid`) at the post-turn `refreshSlot`
 *  rebuild (which carries it) — a mass remount per turn end. */
/** Tie-break suffix for colliding virtualizer keys. Key plumbing only — the
 *  string never renders as user-visible text. */
const DUP_KEY_SUFFIX = '~#'

export function uniqueRowKeys(
  items: readonly DisplayItem[],
  msgKey: (m: ChatMessage) => string,
): string[] {
  const seen = new Map<string, number>()
  return items.map((it, i) => {
    const base = virtualKeyFor(it, i, msgKey, i === items.length - 1)
    const n = seen.get(base)
    if (n === undefined) {
      seen.set(base, 1)
      return base
    }
    // The suffixed candidate is re-checked against `seen` too: a NATURAL key
    // can spell `<base>~#1` (msgKey passes through arbitrary meta), so
    // emitting the suffix unchecked would reintroduce the duplicate this
    // function exists to remove.
    let count = n
    let candidate = `${base}${DUP_KEY_SUFFIX}${count}`
    while (seen.has(candidate)) {
      count++
      candidate = `${base}${DUP_KEY_SUFFIX}${count}`
    }
    seen.set(base, count + 1)
    seen.set(candidate, 1)
    return candidate
  })
}

/** React key for a message row's INNER bubble (the virtualizer row key is
 *  virtualKeyFor). Prefer the optimistic client ts (stashed by the steer-echo
 *  reconcile, and stamped at birth on streaming/thinking messages) over the
 *  server ts, so a mid-stream ts overwrite never remounts the bubble.
 *
 *  Role-prefixed for cross-role uniqueness, EXCEPT that 'streaming' normalizes
 *  to 'assistant': finalization (`_done` / `_segment`) mutates the SAME logical
 *  message's role from streaming to assistant, and a role-sensitive key
 *  remounted the bubble at end-of-turn — destroying useSmoothStream's drain
 *  state, so the trailing unrevealed text (a standing ~LAG_SECS of it under the
 *  constant-latency controller) snapped into view instead of finishing its
 *  reveal. Exported for tests. */
export function messageRowKey(m: ChatMessage, i: number): string {
  const keyTs = (m.meta?.clientTs as string | undefined) || m.ts
  const role = m.role === 'streaming' ? 'assistant' : m.role
  return keyTs ? `${role}-${keyTs}` : `${role}-${i}`
}


/** Render user message content with file chips and image markdown. Handles:
 *  - Fresh messages: meta.files present, displayTxt has @relative/path tokens
 *  - Replayed history: no meta.files, content has [attached_file N] /full/path
 *  - Mixed content: images + file attachments in the same message */
export function KnowledgeBubbleChip({ knowledge }: { knowledge: { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <span className="block mb-1">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-center gap-1 text-[11px] text-accent bg-accent/10 rounded px-1.5 py-0.5 border-none cursor-pointer hover:bg-accent/20 transition-colors"
        aria-expanded={expanded}
        aria-label={expanded ? i18nT('pages.chatPage.collapse_knowledge_context') : i18nT('pages.chatPage.expand_knowledge_context')}
      >
        <BookOpen size={12} className="shrink-0" /> {i18nT('pages.chatPage.knowledge_item', { count: knowledge.items })} · {fmtNumber(knowledge.tokens)} {i18nT('pages.chatPage.tokens')}
      </button>
      {expanded && knowledge.content && (
        <div className="mt-1 max-h-[300px] overflow-auto rounded border border-border bg-bg-elevated p-2 text-[11px]">
          {knowledge.content.map((item, i) => (
            <div key={i} className="mb-2 last:mb-0">
              <div className="font-medium text-text-strong">{item.title}</div>
              <pre className="mt-0.5 whitespace-pre-wrap text-muted font-mono leading-[1.4]" style={{ wordBreak: 'break-word' }}>{item.text}</pre>
            </div>
          ))}
        </div>
      )}
    </span>
  )
}

/** One options object for the user-message render helpers (renderUserContent →
 *  renderUserContentInner → renderFileSegment) instead of ever-growing
 *  positional signatures. The optional session triple mirrors what the
 *  assistant / note rows hand MarkdownRenderer, so a `/chat?sid=…` link in a
 *  USER message switches session in place exactly like every other row kind
 *  (#8253) instead of falling into the external-link branch and gaining
 *  `target="_blank"`. */
export type UserContentRenderOpts = {
  content: string
  meta?: Record<string, unknown>
  onFileOpen: (path: string) => void
  onFolderOpen?: (path: string) => void
  linkPreviews?: boolean
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
}

/** renderFileSegment's own two knobs live on a private extension, not on the
 *  exported type: renderUserContentInner sets both unconditionally, so a
 *  caller-supplied value would type-check and silently do nothing. */
type FileSegmentOpts = UserContentRenderOpts & {
  /** React key namespace for the segment. */
  keyBase?: string
  /** Folder-token label → path map. */
  dirMap?: Map<string, string>
}

export function renderUserContent(opts: UserContentRenderOpts) {
  // Per-message containment (defense-in-depth): a render crash in a
  // user/inject bubble must degrade to a per-message fallback, not unwind to
  // the root boundary and blank the whole dashboard.
  //
  // Sent-prompt images render small: renderFileSegment passes `compactImages`
  // to MarkdownRenderer, which owns the CompactImagesCtx provider internally.
  // (Done there, not here, so tests that mock MarkdownRenderer don't need the
  // context export.)
  return (
    <MessageErrorBoundary rawContent={opts.content}>
      {renderUserContentInner(opts)}
    </MessageErrorBoundary>
  )
}

function renderUserContentInner(opts: UserContentRenderOpts) {
  const { meta, onFileOpen, onFolderOpen } = opts
  let content = opts.content
  const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
  const knowledge = meta?.knowledge as { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } | undefined

  // Folder references resolve FIRST, on the whole message: `[attached_dir N]
  // /path` markers (history replay / steer echo) rewrite to `@label/` display
  // tokens, and fresh `@rel/` tokens map to their meta.dirs path. One pass
  // here — before the paste split — so every segment renderer below sees the
  // token form and one shared label->path map. Dir markers never appear
  // inside paste blocks (they serialize from the typed text only), so the
  // rewrite cannot break paste-token ranges recomputed on the result.
  const { display: dirResolved, dirMentionMap } = resolveDirSegment(content, parseDirs(content, meta))
  content = dirResolved

  const knowledgeBadge = knowledge ? (
    <KnowledgeBubbleChip knowledge={knowledge} />
  ) : null

  if (!pastes.length) return <>{knowledgeBadge}{renderFileSegment({ ...opts, content, keyBase: 'seg', dirMap: dirMentionMap })}</>


  // History load re-serves the fully-EXPANDED content (what the LLM saw), so a
  // message whose bubble was a `[ Paste #N ]` chip when sent comes back as the
  // raw paste text with no token in it. If mergePreservedPastes couldn't
  // re-collapse it (no optimistic bubble, side-table entry evicted/missing),
  // handing that raw text — potentially hundreds of KB / tens of thousands of
  // lines — to renderFileSegment → MarkdownRenderer parses and lays it out on
  // the main thread and freezes the tab. Re-collapse deterministically from the
  // blocks that travel with the message so the chip is restored regardless of
  // external state. See recollapsePastes.
  let text = content
  let ranges = findTokenRanges(text, pastes)
  if (!ranges.length) {
    const collapsed = recollapsePastes(content, pastes)
    if (collapsed !== content) {
      text = collapsed
      ranges = findTokenRanges(text, pastes)
    }
  }
  if (!ranges.length) return <>{knowledgeBadge}{renderFileSegment({ ...opts, content: text, keyBase: 'seg', dirMap: dirMentionMap })}</>

  // Paste chips are inline by nature, so to keep them flowing with the
  // surrounding text (e.g. "hey [chip] thanks"), render each text segment
  // inline — preserves whitespace and doesn't wrap text in a <p> the way
  // MarkdownRenderer does. Trade-off: block-level markdown (lists, code
  // blocks, headings) inside a message that also contains a paste will
  // render as literal text. That's a rare combination for user messages.
  const out: React.ReactNode[] = []
  let lastIdx = 0
  ranges.forEach((r, i) => {
    // Consume one newline on each side of the token so the chip (inline) and
    // its expanded block absorb the line-break that ChatInput.handlePaste
    // forces around the token. Without this, expanding the chip adds an extra
    // visible line (its own block-level display + the still-rendered \n).
    const trimStart = text[r.start - 1] === '\n' ? r.start - 1 : r.start
    const trimEnd = text[r.end] === '\n' ? r.end + 1 : r.end
    if (trimStart > lastIdx) {
      const seg = text.slice(lastIdx, trimStart)
      if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, `t${i}`, dirMentionMap, onFolderOpen))
    }
    out.push(<PastedChip key={`p${i}-${r.block.id}`} block={r.block} />)
    lastIdx = trimEnd
  })
  if (lastIdx < text.length) {
    const seg = text.slice(lastIdx)
    if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, 'tend', dirMentionMap, onFolderOpen))
  }

  // Attachments never referenced by any segment (e.g. an upload with no inline
  // token in the caption) belong to the MESSAGE, not any one segment — render
  // them once here as cards so a multi-segment paste message can't duplicate
  // them (see resolveFileSegment: cardPaths is deliberately segment-scoped).
  // findUnreferencedAttachments owns the referenced/unreferenced decision with
  // the SAME original-list token indexing resolveFileSegment uses (single
  // source of truth; token N indexes the original list, not image-filtered).
  const orderedFiles = parseFiles(text, meta)
  const unreferenced = orderedFiles.length ? findUnreferencedAttachments(text, orderedFiles) : []
  if (unreferenced.length) {
    const labels = buildFileLabels(unreferenced)
    out.push(
      <div key="msg-cards" className="flex flex-col gap-1.5 mt-1">
        {unreferenced.map((p, i) => (
          <FileAttachmentCard key={`msg-c${i}`} fullPath={p} label={labels.get(p) || p} onFileOpen={onFileOpen} />
        ))}
      </div>,
    )
  }
  return knowledgeBadge ? <>{knowledgeBadge}{out}</> : out
}

/** Boundary-checked presence of an `@token` in a text segment — the same rule
 *  the split regex uses, so a key is only offered to a segment that can
 *  actually match it. */
function tokenPresent(text: string, token: string): boolean {
  const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(^|\\s)@${esc}(?=\\s|$)`).test(text)
}

/** Inline chip for a folder reference in a sent message. Clicking opens the
 *  directory in the side panel's file tree — the SAME handler assistant-message
 *  directory chips use (handleFolderOpen -> tabsCtl.openFolder), so a folder is
 *  equally actionable whichever side of the conversation names it. Shift-click
 *  reveals in the OS file manager, mirroring MarkdownRenderer's activatePath.
 *  Without a handler (export used outside ChatPage) it degrades to an inert
 *  span with the path in the tooltip. */
function DirChip({ label, fullPath, onOpen }: { label: string; fullPath: string; onOpen?: (path: string) => void }) {
  // A failed Shift+click reveal renders beside the chip (askAgent on: a sent
  // message's chip holds no draft; the composer draft is persisted per slot).
  const reveal = useRevealFailure(fullPath)
  const body = (
    <>
      <Folder size={11} aria-hidden="true" className="shrink-0 lucide-inline" />@{label}
    </>
  )
  if (!onOpen) {
    return (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded border border-accent/25 bg-accent/10 text-accent text-[12px] font-mono" title={fullPath}>
        {body}
      </span>
    )
  }
  return (
    <>
    <Clickable
      className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
      title={fullPath}
      aria-label={i18nT('pages.chatPage.open_folder', { path: fullPath })}
      onClick={e => {
        // Shift+click copies the path on a remote session. Route through the
        // shared helper, not bare `api.revealPath`: the transport call is
        // side-effect-free, so the helper is what writes the clipboard (and,
        // locally, drives the file manager). A bare call would silently copy
        // nothing and break the chip's hover promise.
        if (e && 'shiftKey' in e && e.shiftKey) { void revealOrOpen(fullPath, 'reveal', reveal); return }
        onOpen(fullPath)
      }}
    >
      {body}
    </Clickable>
    {reveal.error && (
      <ErrorNotice variant="inline" className="ml-1 align-baseline" message={reveal.error} askAgent onDismiss={reveal.clear} testId="dir-chip-reveal-error" />
    )}
    </>
  )
}

/** Inline-flow renderer for a text segment adjacent to a paste chip.
 *  Handles @-file tokens as inline chips; other text is rendered as a
 *  whitespace-preserving span (no markdown). */
function renderInlineSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string, dirMap?: Map<string, string>, onFolderOpen?: (path: string) => void) {
  const parsedFiles = parseFiles(content, meta)
  const dirKeys = dirMap ? [...dirMap.keys()].filter(k => tokenPresent(content, k)).slice(0, 20) : []
  if (!parsedFiles.length && !dirKeys.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{content}</span>
  }
  // Inline-flow variant (adjacent to a paste chip): keep everything inline.
  // Non-image attachments referenced in the text render as inline chips; any
  // standalone-token upload in this segment also renders as an inline chip
  // appended to it (this path can't host block cards without breaking the
  // inline flow). Never-referenced attachments are handled once at message
  // level. Pass the ORIGINAL ordered list so token indices line up.
  const { display, mentionMap, cardPaths, labels } = resolveFileSegment(content, parsedFiles)
  if (!mentionMap.size && !cardPaths.length && !dirKeys.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{display}</span>
  }

  // Folder tokens join the same split as file mentions. A dir key always ends
  // in `/` and a file key never does, so classification below is unambiguous.
  const keys = [...[...mentionMap.keys()].slice(0, 20), ...dirKeys]
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = tokPattern
    ? display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
    : [display]
  const chipCls = 'inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors'
  return (
    <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const dirPath = tok && dirMap?.get(tok)
        if (dirPath) {
          return <DirChip key={`${keyBase}-d${i}`} label={tok} fullPath={dirPath} onOpen={onFolderOpen} />
        }
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className={chipCls} title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}>@{tok}</Clickable>
          )
        }
        return <span key={`${keyBase}-p${i}`}>{part}</span>
      })}
      {cardPaths.map((p, i) => (
        <Clickable key={`${keyBase}-uc${i}`} className={chipCls} title={p} onClick={() => onFileOpen(p)} aria-label={i18nT('pages.chatPage.open_file', { path: p })}>@{labels.get(p) || p}</Clickable>
      ))}
    </span>
  )
}

/** Block card for a single user-attached (non-image) file. Clickable to open
 *  the file via the shared onFileOpen callback. Styled after the agent-side
 *  download card (see components/FileCard.tsx) but carries no size/mime — a
 *  user attachment only has a path here. */
function FileAttachmentCard({ fullPath, label, onFileOpen }: { fullPath: string; label: string; onFileOpen: (path: string) => void }) {
  return (
    <Clickable
      className="flex items-center gap-2.5 max-w-full bg-card border border-border rounded-lg px-3 py-2 text-sm no-underline text-text hover:border-accent transition-colors cursor-pointer animate-scale-in"
      title={fullPath}
      onClick={() => onFileOpen(fullPath)}
      aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}
    >
      <Paperclip size={15} className="shrink-0 text-muted" />
      <span className="font-medium truncate">{label}</span>
    </Clickable>
  )
}

/** File-card + markdown rendering for a text segment (no paste tokens inside).
 *
 *  Attachment display is resolved by the shared resolveFileSegment helper
 *  (utils/fileTokens.ts), the single owner of attachment-marker knowledge —
 *  the same helper backs renderInlineSegment, so the two paths never diverge.
 *  It ALWAYS rewrites the LLM-facing `[attached_file N] /path` plumbing to an
 *  `@label` token (so raw tokens never leak as text) and recovers pre-existing
 *  `@relative` mentions. This handles the persisted-message shape where the
 *  server stores the token form in `content` AND keeps `meta.files` at once.
 *  Files referenced inline stay inline chips; the rest become block cards.
 *  Images keep their inline `![image](path)` markdown and are excluded here. */
function renderFileSegment(opts: FileSegmentOpts) {
  const { content, meta, onFileOpen, keyBase = 'seg', dirMap, onFolderOpen, linkPreviews, onSessionOpen, sessions, activeSession } = opts
  const parsedFiles = parseFiles(content, meta)
  const dirKeys = dirMap ? [...dirMap.keys()].filter(k => tokenPresent(content, k)).slice(0, 20) : []

  // No attachments — plain markdown (bold, code, links, etc.).
  // softBreaks: preserve Shift+Enter line breaks as <br> (see MarkdownRenderer).
  // compactImages: this is user-message content, so attached images render small.
  // linkPreviews: mirrors the assistant path — a URL the user pasted unfurls
  // under the same opt-in gate as one the model wrote (issue #2580).
  // The session triple mirrors the assistant path too, so a `/chat?sid=…`
  // link switches session in place instead of opening a new tab (#8253).
  //
  // A folder token routes the message into the inline chip-split body below,
  // which renders surrounding text as plain whitespace-preserving spans — so
  // markdown in a folder-referencing message shows literally. This is the
  // same trade-off inline file mentions already make, accepted here because
  // the chip must sit inline in the sentence and MarkdownRenderer has no
  // inline-widget seam; a folder-referencing prompt with block markdown is
  // the uncommon combination.
  if (!parsedFiles.length && !dirKeys.length) {
    return <MarkdownRenderer content={content} softBreaks compactImages linkPreviews={linkPreviews} onSessionOpen={onSessionOpen} sessions={sessions} activeSession={activeSession} />
  }

  // Pass the ORIGINAL ordered list (images included) so [attached_file N] token
  // indices line up; resolveFileSegment filters images out of its output.
  const { display, mentionMap, cardPaths, labels } = resolveFileSegment(content, parsedFiles)

  // renderFileSegment handles the WHOLE message (non-paste path), so every
  // attachment belongs to this segment. Cards = standalone-upload tokens in the
  // text PLUS any attachment never referenced at all (e.g. optimistic
  // empty-caption bubble whose content carries no token yet). The
  // never-referenced set is computed by the shared findUnreferencedAttachments
  // (same original-list indexing), deduped against tokens already carded here.
  // Folder references never card: a folder is a path reference, not an upload,
  // and its token is by construction present in the text.
  const carded = new Set(cardPaths)
  const allCardPaths = [
    ...cardPaths,
    ...findUnreferencedAttachments(display, parsedFiles).filter(p => !carded.has(p)),
  ]

  const cards = allCardPaths.length ? (
    <div key={`${keyBase}-cards`} className="flex flex-col gap-1.5 mt-1 first:mt-0">
      {allCardPaths.map((p, i) => (
        <FileAttachmentCard key={`${keyBase}-c${i}`} fullPath={p} label={labels.get(p) || p} onFileOpen={onFileOpen} />
      ))}
    </div>
  ) : null

  // No inline @-mentions of either kind: caption (if any) is plain markdown,
  // then the cards.
  if (!mentionMap.size && !dirKeys.length) {
    const caption = display.trim()
    return <>{caption ? <MarkdownRenderer key={`${keyBase}-cap`} content={caption} softBreaks compactImages linkPreviews={linkPreviews} onSessionOpen={onSessionOpen} sessions={sessions} activeSession={activeSession} /> : null}{cards}</>
  }

  // Inline-mention path: the caption keeps files inline, so render it as a
  // single inline flow — text runs as whitespace-preserving spans (NOT block
  // MarkdownRenderer, which wraps each run in a <p> and would break the line
  // around the chip) and each @token as an inline chip. Block markdown (bold,
  // lists) inside a caption that also carries an inline mention renders as
  // literal text — a rare combination, same trade-off as renderInlineSegment.
  // Cap tokens to prevent ReDoS from many alternations. Folder tokens join
  // the same split; a dir key always ends in `/` and a file key never does,
  // so classification below is unambiguous.
  const keys = [...[...mentionMap.keys()].slice(0, 20), ...dirKeys]
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
  const body = (
    <span key={`${keyBase}-body`} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const dirPath = tok && dirMap?.get(tok)
        if (dirPath) {
          return <DirChip key={`${keyBase}-d${i}`} label={tok} fullPath={dirPath} onOpen={onFolderOpen} />
        }
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
              title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}>@{tok}</Clickable>
          )
        }
        return part ? <span key={`${keyBase}-p${i}`}>{part}</span> : null
      })}
    </span>
  )
  return <>{body}{cards}</>
}
