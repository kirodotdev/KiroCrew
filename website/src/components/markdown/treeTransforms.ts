import type { Root as HastRoot, RootContent, Element as HastElement } from 'hast'

/**
 * Structural passes over the parsed trees: the ones that keep React's tree equal
 * to the one the browser builds (`rehypeMarkFencedCode`, `rehypeUnwrapBlocks`),
 * soft line breaks for surfaces where a lone newline is meaningful
 * (`remarkSoftBreaks`), source positions for inline commenting
 * (`rehypeSourcepos`), and position-stable block keys (`rehypeStableRootKeys`).
 * Their order in the chains is decided in `MarkdownRenderer.tsx`.
 */

/**
 * HTML block-level elements that cannot legally nest inside `<p>`. When
 * `rehype-raw` parses raw HTML embedded in markdown, it may produce a HAST tree
 * with a block element inside a `<p>` (e.g. `<p><div>…</div></p>`). The
 * browser's HTML parser auto-corrects this by closing the `<p>` before the
 * block element, moving the block out — but React's VDOM still thinks the block
 * is inside the `<p>`. On the next reconciliation React tries to `removeChild`
 * from `<p>`, the node is no longer there, and we get:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * This plugin mirrors the browser's correction at the HAST level so React's
 * tree matches reality from the first render.
 */
const BLOCK_ELEMENTS = new Set([
  'address', 'article', 'aside', 'blockquote', 'details', 'dialog', 'dd',
  'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'li',
  'main', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul',
])

/**
 * Stamps `data-fenced` on every `<code>` that is the child of a `<pre>`.
 *
 * `MD_COMPONENTS.code` renders a block-level component for a code element that
 * carries a class (`CodeBlock`, `MermaidBlock` and `ExcalidrawBlock` are each
 * rooted in a `<div>`). Real fenced blocks never reach it — `useBlockAssembler`
 * segments those out of the source and `BlockRenderer` draws them directly — so
 * the only classed code elements arriving here come from raw HTML in prose.
 * `<pre><code class="language-js">` is the legitimate shape: the `<pre>` is
 * block-level, so `rehypeUnwrapBlocks` hoists it clear of any surrounding `<p>`
 * and the block renders as a sibling.
 *
 * A BARE `<code class="language-js">` mid-sentence is not. The sanitizer
 * allowlists `class` globally (see GLOBAL_ATTRS), so it survives, keeps its
 * class, and renders a `<div>` inside the enclosing `<p>`. The browser hoists
 * that `<div>` out of the `<p>`, React's VDOM does not follow, and the next
 * reconciliation throws:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * `rehypeUnwrapBlocks` cannot catch this, because it decides block-ness from the
 * HAST tag name and `code` is inline there — the block only appears in what the
 * component renders. Marking the genuinely fenced ones lets the override keep
 * inline code inline whatever class it carries, which is also what the source
 * asked for.
 */
export function rehypeMarkFencedCode() {
  return (tree: HastRoot) => {
    const walk = (node: HastRoot | HastElement) => {
      if (!node.children) return
      const isPre = node.type === 'element' && node.tagName === 'pre'
      for (const child of node.children) {
        if (child.type !== 'element') continue
        // The marker is ours to set and no one else's. `isAllowedAttr` admits
        // every `data-*`, so raw HTML in the message can carry its own
        // `data-fenced` — and an inline `<code data-fenced class="language-js">`
        // would then claim block rendering and reintroduce the very crash this
        // plugin exists to prevent.
        //
        // Deleting a fixed key spelling is not enough. The HTML parser
        // lowercases attribute names, so `dataFenced` arrives as the hast
        // property `datafenced`, which the JSX serializer still hands to the
        // component as `data-fenced`. Strip by NORMALIZED form so every casing
        // and dash placement that can reach the override as the marker is
        // removed here.
        if (child.properties) {
          for (const key of Object.keys(child.properties)) {
            if (key.toLowerCase().replace(/-/g, '') === 'datafenced') {
              delete child.properties[key]
            }
          }
        }
        if (isPre && child.tagName === 'code') {
          child.properties = { ...(child.properties ?? {}), 'data-fenced': '' }
        }
        walk(child)
      }
    }
    walk(tree)
  }
}

export function rehypeUnwrapBlocks() {
  return (tree: HastRoot) => {
    const walk = (parent: HastRoot | HastElement) => {
      if (!parent.children) return
      for (let i = 0; i < parent.children.length; i++) {
        const child = parent.children[i]
        if (child.type === 'element') walk(child)
      }
      // Only `<p>` elements need unwrapping (that's the only element the
      // browser auto-closes when it encounters a block child).
      if (parent.type !== 'element' || parent.tagName !== 'p') return
      const hasBlock = parent.children.some(
        c => c.type === 'element' && BLOCK_ELEMENTS.has(c.tagName),
      )
      if (!hasBlock) return

      // Split: children before a block go into a <p>, the block becomes a
      // sibling, children after go into the next iteration's bucket. We
      // rebuild the parent's slot in-place by replacing it in the grandparent.
      // Since we're walking depth-first and only mutate the CURRENT parent's
      // children list at the grandparent level, we handle this by returning
      // replacement nodes and letting the outer walk splice them.
      const replacement: RootContent[] = []
      let bucket: RootContent[] = []
      const flushBucket = () => {
        // Only emit a <p> wrapper if the bucket has non-whitespace content.
        const hasContent = bucket.some(n =>
          n.type === 'element' || (n.type === 'text' && n.value.trim()),
        )
        if (hasContent) {
          replacement.push({
            type: 'element',
            tagName: 'p',
            properties: { ...(parent as HastElement).properties },
            children: bucket as HastElement['children'],
            // Preserve source position so rehypeSourcepos can stamp
            // data-sourcepos on the synthesized wrappers (needed for
            // inline-comment anchoring).
            position: (parent as HastElement).position,
          })
        }
        bucket = []
      }
      for (const child of parent.children) {
        if (child.type === 'element' && BLOCK_ELEMENTS.has(child.tagName)) {
          flushBucket()
          replacement.push(child as RootContent)
        } else {
          bucket.push(child as RootContent)
        }
      }
      flushBucket()
      // Stash the replacement so the caller can splice it.
      ;(parent as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement = replacement
    }

    // Two-pass: first walk marks <p> elements that need splitting, then we
    // splice replacements into their parents top-down. A single pass that
    // mutates children while iterating would skip indices.
    const splice = (node: HastRoot | HastElement) => {
      if (!node.children) return
      let i = 0
      while (i < node.children.length) {
        const child = node.children[i]
        if (child.type === 'element') splice(child)
        const rep = (child as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement
        if (rep) {
          delete (child as HastElement & { _unwrapReplacement?: RootContent[] })._unwrapReplacement
          ;(node.children as RootContent[]).splice(i, 1, ...rep)
          i += rep.length
        } else {
          i++
        }
      }
    }

    walk(tree)
    splice(tree)
  }
}

// Matches one source line break plus any leading tabs/spaces, so a trailing
// space before the break doesn't survive as its own text node. Mirrors the
// pattern used by the `remark-breaks` package.
const SOFT_BREAK_RE = /[\t ]*(?:\r?\n|\r)/g

/**
 * remark plugin: turn soft line breaks (a lone source newline inside a
 * paragraph, which CommonMark otherwise collapses to a space) into hard breaks
 * (mdast `break` → <br>). This is an inlined equivalent of the `remark-breaks`
 * package, kept local to avoid adding a runtime dependency.
 *
 * Opt-in via MarkdownRenderer's `softBreaks` prop, for surfaces where a lone
 * source newline is meaningful: user messages (Shift+Enter in the composer)
 * and injected notes. Assistant/LLM markdown keeps standard CommonMark
 * soft-break-collapse.
 *
 * Operates on `text` nodes only, so fenced code, inline code, math, and raw
 * HTML (whose content lives in `.value`, not `.children`) are untouched, and
 * blank-line block separators — already parsed as distinct blocks — are not
 * affected, so lists and paragraphs keep their normal block spacing. That is
 * what lets those surfaces drop container-level `white-space: pre-wrap`, which
 * had made react-markdown's inter-block newline text nodes render as literal
 * blank lines and inflated list/paragraph gaps.
 */
export function remarkSoftBreaks() {
  const visit = (node: { type?: string; value?: string; children?: unknown[] }) => {
    if (!node || !Array.isArray(node.children)) return
    const out: unknown[] = []
    for (const raw of node.children) {
      const child = raw as { type?: string; value?: string; children?: unknown[] }
      if (child.type === 'text' && typeof child.value === 'string' && /[\r\n]/.test(child.value)) {
        const value = child.value
        let start = 0
        SOFT_BREAK_RE.lastIndex = 0
        let match: RegExpExecArray | null
        while ((match = SOFT_BREAK_RE.exec(value))) {
          if (match.index > start) out.push({ type: 'text', value: value.slice(start, match.index) })
          out.push({ type: 'break' })
          start = match.index + match[0].length
        }
        if (start < value.length) out.push({ type: 'text', value: value.slice(start) })
      } else {
        visit(child)
        out.push(child)
      }
    }
    // A break ADJACENT to an image is redundant and inflates spacing: the
    // image renders as its own block (span.block.my-2), so the line break is
    // already implied — the <br> would add an empty line box (~one
    // line-height) AND keep the neighbouring margins from collapsing,
    // turning the intended 8px gap between two attached screenshots into
    // ~37px. Text-to-text breaks (Shift+Enter prose) are untouched.
    const isImage = (n: unknown): boolean => (n as { type?: string })?.type === 'image'
    node.children = out.filter((n, i) => {
      if ((n as { type?: string })?.type !== 'break') return true
      return !(isImage(out[i - 1]) || isImage(out[i + 1]))
    })
  }
  return (tree: unknown) => visit(tree as { children?: unknown[] })
}

/**
 * Rehype plugin that copies each hast element's source `position` onto a
 * `data-sourcepos` HTML attribute in CommonMark format `startLine:startCol-endLine:endCol`.
 * Used by the inline-commenting flow to map selection DOM → source coordinates.
 * Replaces the deprecated `sourcePos` option removed in react-markdown v10.
 */
export function rehypeSourcepos() {
  return (tree: HastRoot) => {
    const walk = (node: HastRoot | RootContent) => {
      if (node.type === 'element' && node.position?.start) {
        const s = node.position.start, e = node.position.end ?? s
        node.properties = node.properties || {}
        node.properties['data-sourcepos'] = `${s.line}:${s.column}-${e.line}:${e.column}`
      }
      if ('children' in node && node.children) for (const c of node.children) walk(c)
    }
    walk(tree)
  }
}

/** Give every root-level block a key that depends on its POSITION, not on the
 *  tags of its siblings.
 *
 *  `hast-util-to-jsx-runtime` keys each child `<tagName>-<count of that tagName
 *  so far>`, so a block's key depends on what its earlier siblings are. When a
 *  later streaming line reclassifies an EARLIER line -- a `===` underline turns
 *  the paragraph above it into a heading -- that earlier sibling stops being a
 *  `p`, every later paragraph's counter shifts down, and React unmounts and
 *  remounts blocks whose own text never changed. A settled paragraph losing its
 *  node loses the reader's selection and restarts its animations, and the browser
 *  lays the replacement out afresh.
 *
 *  Wrapping each root child in one uniform element makes that counter a
 *  positional index, so a block keeps its key for as long as it keeps its place.
 *  `display: contents` leaves the wrapper without a box, so margins, margin
 *  collapsing and descendant selectors see the tree they saw before. Applied on
 *  every render rather than only while streaming: a wrapper that appeared or
 *  vanished when the stream ended would itself remount every block, which is the
 *  thing this prevents.
 */
export function rehypeStableRootKeys() {
  return (tree: HastRoot) => {
    let wrapped = false
    const children = tree.children.map((child): RootContent => {
      if (child.type !== 'element') return child
      wrapped = true
      return {
        type: 'element',
        tagName: 'div',
        properties: { style: 'display: contents' },
        children: [child],
      }
    })
    if (wrapped) tree.children = children
  }
}
