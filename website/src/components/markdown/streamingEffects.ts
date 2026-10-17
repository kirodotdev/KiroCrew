import type { Root as HastRoot, RootContent, Element as HastElement, Text as HastText } from 'hast'

/**
 * What a still-streaming tail looks like: the trailing-word glow, the smooth
 * per-character reveal and the inline caret. Each is a rehype pass on the parsed
 * tree, added only for the live tail block (see `MarkdownBlock`), so a settled
 * message carries none of their nodes.
 */

/** A hast node that owns a `children` array — either the document root or an
 *  element. Both accept `Element`/`Text` children, so our inserted glow/reveal
 *  spans are valid in either. */
type HastParent = HastRoot | HastElement

/** Splice replacement `<span>`/text nodes into a parent's children, replacing
 *  the single node at `index`. Root and Element have differently-typed children
 *  arrays (`RootContent[]` vs `ElementContent[]`) that both admit Element/Text,
 *  so this narrows on the parent kind to keep the splice type-safe. */
function spliceChildren(parent: HastParent, index: number, nodes: Array<HastElement | HastText>): void {
  if (parent.type === 'root') parent.children.splice(index, 1, ...nodes)
  else parent.children.splice(index, 1, ...nodes)
}

/** Number of trailing characters glowed while a message streams. */
export const GLOW_TAIL_CHARS = 30

/**
 * Rehype plugin: wrap the message's trailing text in a
 * `<span class="streaming-glow">` so the newest streamed words shimmer.
 *
 * Operates on the parsed HAST tree (not the markdown source and not the live
 * DOM), so it: (a) never builds a raw HTML string with LLM content — the span
 * is a real element node react-markdown renders as a React `<span>`; (b) never
 * bisects a markdown token — by this stage `**bold**` is already a `<strong>`
 * element, so splitting the last *text* node is always safe; (c) doesn't mutate
 * React-owned DOM, so it can't cause reconciliation crashes.
 *
 * Glows the whole last text node when it's short, else its last GLOW_TAIL_CHARS
 * on a space boundary (never mid-word). Skips text inside code/pre.
 */
export function rehypeStreamingGlow(options?: { tailChars?: number }) {
  const tailChars = options?.tailChars ?? GLOW_TAIL_CHARS
  return (tree: HastRoot) => {
    // Collect every eligible text node (non-whitespace, not inside code/pre);
    // the streaming tail is the last one. Using an array (rather than a
    // closure-mutated `let`) keeps TypeScript's control-flow narrowing happy.
    const candidates: { parent: HastParent; index: number; value: string }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number, inCode: boolean) => {
      if (node.type === 'text') {
        if (!inCode && node.value && node.value.trim()) {
          candidates.push({ parent, index, value: node.value })
        }
        return
      }
      const code = inCode || (node.type === 'element' && (node.tagName === 'code' || node.tagName === 'pre'))
      if ('children' in node && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i, code)
      }
    }
    for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i, false)
    const target = candidates[candidates.length - 1]
    if (!target) return
    const { parent, index, value } = target
    let cut: number
    if (value.length <= tailChars) {
      cut = 0
    } else {
      const sp = value.lastIndexOf(' ', value.length - tailChars)
      cut = sp > 0 ? sp : value.length - tailChars
    }
    const before = value.slice(0, cut)
    const tail = value.slice(cut)
    if (!tail.trim()) return
    const span: HastElement = {
      type: 'element',
      tagName: 'span',
      properties: { className: ['streaming-glow'] },
      children: [{ type: 'text', value: tail }],
    }
    const beforeNode: HastText = { type: 'text', value: before }
    spliceChildren(parent, index, before ? [beforeNode, span] : [span])
  }
}

/** Split a text run into individual characters for per-char animation. */
const REVEAL_CHAR_RE = /[\s\S]/g

/** How many trailing characters of the streaming tail carry the reveal fade.
 *  Only this growing EDGE is sub-opaque; text that has settled behind it is
 *  left as plain, fully-opaque text nodes. Sized to comfortably cover the
 *  smooth buffer's per-frame reveal wave (MAX_CPS burst) so genuinely-new text
 *  still materializes over several frames. */
const REVEAL_FADE_CHARS = 32
/** Opacity of the newest (tip) character; older chars ramp linearly to 1 across
 *  REVEAL_FADE_CHARS. Kept well above 0 so a mid-stream PAUSE never leaves the
 *  trailing words hard to read — the reveal is a gentle materialization, not a
 *  fade-from-invisible. */
const REVEAL_MIN_OPACITY = 0.6

/** How long the rendered content must sit unchanged before the reveal edge is
 *  settled to full opacity. `--ft-o` is POSITIONAL, so only the tip advancing
 *  raises a character's opacity. This matters for exactly one case: a stream
 *  that PAUSES mid-turn (the gap while the model composes tool arguments), where
 *  `streaming` is still true and nothing advances the tip, leaving the last
 *  REVEAL_FADE_CHARS characters pinned as low as REVEAL_MIN_OPACITY for the
 *  whole pause. A FINISHED stream is already self-healing and needs nothing:
 *  rehypeStreamingReveal is only in the pipeline while `glow` is set, and
 *  `glow` follows `isStreaming`, so the spans are dropped on the next re-parse.
 *  Do not "simplify" this into `animOn = !!smooth && streaming` — that only
 *  covers the self-healing case and cannot cover a pause, where streaming is
 *  true by definition. */
export const REVEAL_IDLE_SETTLE_MS = 500

/** Opacity for a character `d` positions back from the streaming tip (d=0 is
 *  the newest char). Deliberately a pure function of POSITION, not of mount
 *  time — this is the streaming-flash fix. react-markdown re-parses the whole
 *  tail every frame, and when a newly-revealed char COMPLETES a markdown token
 *  (inline `code`, **bold**, a [link], a heading/list marker, …) the subtree
 *  restructures, so React unmounts/remounts the `.ft-word` spans for text that
 *  was ALREADY on screen. A mount-triggered CSS keyframe (like `ft-char-fade`)
 *  would re-run on every such remount → a visible flash, right at the active
 *  edge where the eye is. With position-derived opacity a
 *  remounted span re-appears at the IDENTICAL opacity, so it cannot re-fade;
 *  only the tip advancing changes a char's opacity, giving a smooth
 *  materialization. Confirmed by src/test/streamingFlashRepro.test.tsx. */
function revealOpacity(d: number): number {
  if (d >= REVEAL_FADE_CHARS - 1) return 1
  const o = REVEAL_MIN_OPACITY + (1 - REVEAL_MIN_OPACITY) * (d / (REVEAL_FADE_CHARS - 1))
  return Math.round(o * 100) / 100
}

/**
 * Rehype plugin: wrap the streaming tail's TRAILING EDGE in `<span
 * class="ft-word" style="--ft-o:…">` so each character carries a
 * position-derived opacity (see revealOpacity). Only the last
 * REVEAL_FADE_CHARS characters are wrapped; text that has settled behind the
 * edge stays as plain, fully-opaque text nodes.
 *
 * Text inside `code`/`pre` (rendered by the code components) and
 * `.streaming-glow` is skipped. Atomic block components (fenced code, widgets,
 * mermaid, diffs) are separate non-text blocks and are not faded here.
 *
 * The reveal is driven by CSS opacity that is a pure function of each char's
 * distance to the tip — NOT a mount-triggered animation — so react-markdown's
 * per-frame re-parse (which remounts edge spans whenever a markdown token
 * completes) can never re-fire the fade on already-visible text. That
 * remount-immunity is the streaming-flash fix. This plugin runs AFTER
 * rehypeSanitize in the pipeline, so the inline `--ft-o` style it adds is not
 * stripped by the attribute allowlist. On stream end the plugin drops out and
 * the tail reverts to plain text (clean for selection/copy).
 */
export function rehypeStreamingReveal() {
  return (tree: HastRoot) => {
    const candidates: { parent: HastParent; index: number; value: string }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number, skip: boolean) => {
      if (node.type === 'text') {
        if (!skip && node.value && node.value.trim()) {
          candidates.push({ parent, index, value: node.value })
        }
        return
      }
      const cls = node.type === 'element' ? node.properties?.className : undefined
      const isGlow = Array.isArray(cls) && cls.includes('streaming-glow')
      // Skip text inside `pre` (fenced code/diff render via their own
      // components) and the glow window. Inline `code` is NOT skipped so it
      // char-fades like the surrounding prose — fenced blocks are separate
      // non-markdown blocks, so any `code` reached here is inline.
      const next = skip || isGlow || (node.type === 'element' && node.tagName === 'pre')
      if ('children' in node && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i, next)
      }
    }
    for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i, false)
    if (candidates.length === 0) return
    // Wrap only the trailing REVEAL_FADE_CHARS characters, walking candidates
    // from the last (deepest in document order) backward and spending a shared
    // budget. Everything before the edge is left as-is (plain text). `fromEnd`
    // tracks how many wrapped chars lie AFTER the current candidate so each
    // span gets an opacity derived from its distance to the streaming tip.
    let budget = REVEAL_FADE_CHARS
    let fromEnd = 0
    for (let c = candidates.length - 1; c >= 0 && budget > 0; c--) {
      const { parent, index, value } = candidates[c]
      // Keep the leading (settled) portion of the boundary node as a plain text
      // node; only wrap its trailing chars. A char-exact cut is fine because
      // opacity is continuous — the boundary char lands at ~1.0, matching the
      // adjacent plain text, so there is no visible seam.
      const cut = value.length > budget ? value.length - budget : 0
      budget -= (value.length - cut)
      const head = value.slice(0, cut)
      const tail = value.slice(cut)
      const tokens = tail.match(REVEAL_CHAR_RE)
      if (!tokens || tokens.length === 0) continue
      // tokens are in document order; the last token of the last candidate is
      // the tip. distance-from-tip for tokens[i] = fromEnd + (last - i).
      const spans: Array<HastElement | HastText> = tokens.map((tok, i) => ({
        type: 'element',
        tagName: 'span',
        properties: { className: ['ft-word'], style: `--ft-o:${revealOpacity(fromEnd + (tokens.length - 1 - i))}` },
        children: [{ type: 'text', value: tok }],
      }))
      fromEnd += tokens.length
      // Splice highest index first (candidates ascend in document order, so
      // walking c downward gives descending indices within a shared parent),
      // keeping earlier candidates' indices valid.
      spliceChildren(parent, index, head ? [{ type: 'text', value: head } as HastText, ...spans] : spans)
    }
  }
}

/**
 * Rehype plugin: append an inline blinking caret (`<span class="streaming-caret">`)
 * immediately after the message's LAST trailing text node, so it sits inline at
 * the end of the streamed text (on the same line as the final word) rather than
 * on a new line below the block.
 *
 * Runs only while streaming (added under MarkdownBlock's `glow` gate, which is
 * true only for the last markdown block), so exactly one caret is injected. The
 * caret is a childless element node — the glow/reveal plugins that run after it
 * only touch text nodes, so it is left untouched and the trailing text still
 * gets its shimmer/fade. On stream end the plugin drops out and the caret
 * disappears with no leftover node (clean for selection/copy).
 *
 * Falls back to appending at the tree root only when there is no eligible text
 * yet (e.g. the block is pure code) — a rare edge where a new-line caret is
 * acceptable.
 */
export function rehypeStreamingCaret() {
  return (tree: HastRoot) => {
    const candidates: { parent: HastParent; index: number }[] = []
    const walk = (node: RootContent, parent: HastParent, index: number) => {
      if (node.type === 'text') {
        if (node.value && node.value.trim()) candidates.push({ parent, index })
        return
      }
      // Block code: exclude entirely — the caret never belongs inside a fenced
      // code/diff block.
      if (node.type === 'element' && node.tagName === 'pre') return
      // Inline code: record the <code> element itself as a candidate at the
      // PARENT level (and don't recurse into its text children), so the caret
      // lands AFTER the inline code, not before it. Without this, a message
      // ending in `` `code` `` would splice the caret ahead of the <code>.
      if (node.type === 'element' && node.tagName === 'code') { candidates.push({ parent, index }); return }
      if (node.type === 'element' && node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i)
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i)
    }
    const caret: HastElement = {
      type: 'element',
      tagName: 'span',
      properties: { className: ['streaming-caret'], 'aria-hidden': 'true' },
      children: [],
    }
    const target = candidates[candidates.length - 1]
    if (target) {
      // Insert as the next sibling of the last visible node (text run or inline
      // <code>) so it renders inline right after the final content. Narrow on
      // the parent kind (RootContent[] vs ElementContent[]) to keep the insert
      // type-safe — spliceChildren removes a node, so it can't do an insert.
      if (target.parent.type === 'root') target.parent.children.splice(target.index + 1, 0, caret)
      else target.parent.children.splice(target.index + 1, 0, caret)
    } else if (tree.children) {
      tree.children.push(caret)
    }
  }
}
