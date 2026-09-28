/**
 * Bound the depth of a parsed markdown tree, using the real parsers as the
 * oracle.
 *
 * Chat markdown is input-controlled. A message of thousands of leading `>`
 * markers, or of progressively indented list items, or of `<div><div>…` raw
 * HTML, parses into a tree whose depth equals the nesting count. Several
 * layers downstream of the parser recurse over that tree -- `remark-gfm`'s
 * own post-parse transform (`unist-util-visit-parents`), `remark-rehype`,
 * `hast-util-from-parse5` inside `rehype-raw`, `react-markdown`'s hast->JSX
 * conversion, and this codebase's own tree walkers -- and past the engine's
 * call-stack limit any one of them throws `RangeError: Maximum call stack
 * size exceeded` while rendering a single message.
 *
 * The parsers themselves are iterative: `micromark` and
 * `mdast-util-from-markdown` parse a 50,000-deep blockquote run without
 * throwing, and `parse5`'s tree builder keeps its own explicit stack. So the
 * depth of the tree is a FACT that can be read off the parser's output, and
 * bounding it there needs no model of CommonMark or of the HTML tokenizer.
 * (Rewriting the raw text before parsing, the alternative, requires exactly
 * such a model, and every rule it gets slightly different from the real
 * parser is a bypass; that approach was tried and could not converge.)
 *
 * Two plugins, one per parser:
 *
 * - `remarkBoundDepth` registers a `mdast-util-from-markdown` transform. That
 *   hook runs INSIDE `parse()`, in registration order, before any unified
 *   transformer -- and `remark-gfm` registers a recursive transform through
 *   the same hook. So this plugin must be `use()`d before `remark-gfm`; a
 *   test pins the order.
 * - `rehypeBoundRawDepth` runs before `rehype-raw`. If the tree holds raw HTML
 *   it serializes the tree and parses it with `parse5` (iterative) to measure
 *   the DOM depth the raw HTML will produce; past the bound, every raw node is
 *   downgraded to text so `hast-util-from-parse5` never sees a deep tree.
 *
 * Below the bound both plugins leave the tree untouched, so ordinary content
 * renders byte-identically and `data-sourcepos` coordinates stay exact.
 *
 * One dimension the tree cannot bound is the PARSER'S OWN COST: micromark
 * re-scans every open container on every line, so a list indented to depth
 * d costs O(lines * d) before any tree exists -- a 600-level list of empty
 * items parses in ~13s, and 1,200 levels for minutes. Indentation is decided
 * by whitespace alone, so `capWhitespaceRuns` truncates any run of spaces
 * and tabs wider than `MAX_INDENT_COLS`, wherever on the line it sits (a
 * run after a `>` prefix indents a list inside a blockquote exactly as a
 * leading run does). It recognizes no construct. No CommonMark construct
 * needs more than four columns of whitespace to mean what it means, so a
 * run of 256 means the same as a run of 300 everywhere except inside code,
 * where alignment past 256 columns loses the excess -- the one visible
 * effect, and only on content no person writes.
 */
import type { Nodes as MdastNodes, Root as MdastRoot } from 'mdast'
import type { Root as HastRoot } from 'hast'
import type { Processor } from 'unified'
import { toHtml } from 'hast-util-to-html'
import { parseFragment } from 'parse5'

/**
 * Maximum tree depth admitted, in nodes from the root. Human-authored content
 * sits far below this (a five-level list inside a blockquote inside a list
 * item is depth ~12); the recursive layers above overflow somewhere past a
 * few thousand. The bound only has to sit between those two.
 */
export const MAX_TREE_DEPTH = 100

/**
 * Maximum width of one whitespace run kept, in columns (tab = 4). A
 * CommonMark list item nests one level per two to four columns, so 256 caps
 * indentation-driven depth around 64-128 -- inside `MAX_TREE_DEPTH` -- and
 * caps micromark's per-line container scan with it.
 */
export const MAX_INDENT_COLS = 256

/**
 * Truncate every run of spaces and tabs wider than `MAX_INDENT_COLS`, on
 * every line, wherever the run sits. Lexical only: no construct is
 * recognized, so there is no grammar to get wrong. Ordinary content has no
 * such run and returns unchanged, by identity.
 */
export function capWhitespaceRuns(text: string): string {
  // Fast paths are by LENGTH, so a tab (one char, up to four columns) opts
  // the text out of them.
  if (text.length <= MAX_INDENT_COLS && !text.includes('\t')) return text
  let out: string[] | null = null
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.length <= MAX_INDENT_COLS && !line.includes('\t')) continue
    let rebuilt: string | null = null
    let emitted = 0
    let j = 0
    const n = line.length
    while (j < n) {
      const c = line.charCodeAt(j)
      if (c !== 32 && c !== 9) {
        j++
        continue
      }
      // Measure the whole run, then replace it as a unit if it is too wide.
      const start = j
      let cols = 0
      while (j < n) {
        const d = line.charCodeAt(j)
        if (d === 32) cols += 1
        else if (d === 9) cols += 4 - (cols % 4)
        else break
        j++
      }
      if (cols > MAX_INDENT_COLS) {
        rebuilt = (rebuilt ?? '') + line.slice(emitted, start) + ' '.repeat(MAX_INDENT_COLS)
        emitted = j
      }
    }
    if (rebuilt === null) continue
    if (!out) out = lines.slice()
    out[i] = rebuilt + line.slice(emitted)
  }
  return out ? out.join('\n') : text
}

type Parent = { children?: unknown[] }

/**
 * Text of a subtree, gathered without recursion, so a folded subtree keeps
 * its words on screen instead of vanishing.
 */
function flatText(node: unknown): string {
  const out: string[] = []
  const stack: unknown[] = [node]
  while (stack.length) {
    const n = stack.pop() as { value?: unknown; alt?: unknown; children?: unknown[] }
    if (typeof n.value === 'string') out.push(n.value)
    else if (typeof n.alt === 'string' && n.alt !== '') out.push(n.alt) // image, imageReference
    const kids = n.children
    if (kids) for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i])
  }
  return out.join(' ')
}

/**
 * Fold every subtree deeper than `MAX_TREE_DEPTH` into one text node. Depth is
 * measured with an explicit stack: this function is itself one of the layers
 * that must not recurse.
 */
function boundMdastDepth(tree: MdastRoot): MdastRoot {
  const stack: Array<[Parent, number]> = [[tree as Parent, 0]]
  while (stack.length) {
    const [node, depth] = stack.pop()!
    const kids = node.children
    if (!kids || kids.length === 0) continue
    if (depth >= MAX_TREE_DEPTH - 1) {
      node.children = [{ type: 'text', value: flatText({ children: kids }) } as MdastNodes]
      continue
    }
    for (const k of kids) stack.push([k as Parent, depth + 1])
  }
  return tree
}

/**
 * unified plugin: bound mdast depth as part of `parse()`. Must precede
 * `remark-gfm` in the plugin list (see the module comment).
 */
export function remarkBoundDepth(this: Processor): void {
  const data = this.data() as { fromMarkdownExtensions?: unknown[] }
  const list = data.fromMarkdownExtensions || (data.fromMarkdownExtensions = [])
  // `mdast-util-from-markdown` runs every extension's `transforms` in order
  // after the tree is built, before `parse()` returns.
  list.unshift({ transforms: [boundMdastDepth] })
}

/**
 * The parse5 options `hast-util-raw` uses (its `parseOptions`, minus source
 * locations, which do not affect the tree). Kept equal so the oracle below
 * reads the same tree the real parse will build.
 */
const RAW_PARSE_OPTIONS = { scriptingEnabled: false }

type P5Node = { childNodes?: unknown[]; content?: { childNodes?: unknown[] } }

/**
 * Depth of a parse5 fragment, measured iteratively. A `<template>` keeps its
 * children on a separate `content` fragment rather than `childNodes`, and
 * `hast-util-from-parse5` recurses into that fragment too, so it counts.
 */
function parse5Depth(fragment: P5Node): number {
  let max = 0
  const stack: Array<[P5Node, number]> = [[fragment, 0]]
  while (stack.length) {
    const [n, d] = stack.pop()!
    if (d > max) max = d
    const kids = n.childNodes
    if (kids) for (const c of kids) stack.push([c as P5Node, d + 1])
    // hast-util-from-parse5 interposes the `content` fragment as its own
    // root node between the template and its children, so those sit two
    // levels down in the tree the recursive layers will walk.
    const inner = n.content?.childNodes
    if (inner) for (const c of inner) stack.push([c as P5Node, d + 2])
  }
  return max
}

/**
 * Bound the depth raw HTML would add. Returns true when raw nodes were
 * downgraded. Exported for the oracle-parity test, which runs this against
 * the tree `rehype-raw` actually builds; the plugin below is the production
 * entry.
 */
export function boundRawHtmlDepth(tree: HastRoot): boolean {
  const raws: Array<{ type: string; value: string }> = []
  const stack: unknown[] = [tree]
  while (stack.length) {
    const n = stack.pop() as { type: string; value?: string; children?: unknown[] }
    if (n.type === 'raw' && typeof n.value === 'string') raws.push(n as { type: string; value: string })
    if (n.children) for (const c of n.children) stack.push(c)
  }
  if (raws.length === 0) return false
  // The oracle: the same HTML `rehype-raw` is about to re-tokenize, parsed by
  // the same parser, minus the recursive hast conversion. `rehype-raw` feeds
  // parse5 the tree's elements as tags and its raw nodes as text, which is
  // what serializing the tree with raw HTML admitted produces.
  //
  // The parser OPTIONS must match too, or the oracle and the real parse read
  // different trees from the same bytes. `hast-util-raw` parses with
  // `scriptingEnabled: false`; under parse5's default (`true`) a `<noscript>`
  // body is RAWTEXT -- flat -- while the real parse nests everything inside
  // it, so a `<noscript>` followed by a deep run would read as depth 2 here
  // and build the full tree there. A test pins the `<noscript>` case.
  const fragment = parseFragment(toHtml(tree, { allowDangerousHtml: true }), RAW_PARSE_OPTIONS)
  if (parse5Depth(fragment) <= MAX_TREE_DEPTH) return false
  // Downgrade: a text node renders as escaped text, so the message keeps its
  // words and loses only its HTML rendering. Only a message that nests past
  // the bound pays this; ordinary raw HTML is untouched.
  for (const r of raws) r.type = 'text'
  return true
}

/** rehype plugin: bound raw-HTML depth. Must precede `rehype-raw`. */
export function rehypeBoundRawDepth() {
  return (tree: HastRoot) => {
    boundRawHtmlDepth(tree)
  }
}
