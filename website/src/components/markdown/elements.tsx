import React from 'react'
import type { Components } from 'react-markdown'
import type { Element as HastElement } from 'hast'
import { TAG_ATTRS } from './sanitize'

/**
 * The element overrides that only restyle, and the attribute forwarding they
 * share. Each override rebuilds its element with the renderer's classes and
 * forwards what the sanitizer admitted (`sp` / `spa`); `MD_COMPONENTS` in
 * `MarkdownRenderer.tsx` spreads them beside the overrides that change what an
 * element renders.
 */

/** Forward the `data-sourcepos` attribute from rehypeSourcepos onto the
 *  rendered element. Used in every MD_COMPONENTS override; returns an
 *  empty-valued attribute when sourcePos is disabled (React omits it from
 *  the DOM). */
export const sp = (node?: HastElement) => {
  const v = node?.properties?.['data-sourcepos']
  return { 'data-sourcepos': typeof v === 'string' ? v : undefined }
}

/** `sp` plus every attribute the sanitize schema admits for `tag`.
 *
 *  An MD_COMPONENTS override rebuilds its element to attach a className, and
 *  a rebuild forwards only what it names. Naming just `sp(node)` silently
 *  dropped every attribute `TAG_ATTRS` had already decided was safe: `<ol
 *  start>` renumbered a fence-split step list back to 1, and a raw-HTML table
 *  with `colspan` was admitted by sanitize and then flattened by the override.
 *  Deriving the forward from the same table the sanitizer consults keeps the
 *  two from drifting again — an attribute added there reaches the DOM without
 *  a second edit here.
 *
 *  `className` is excluded because the override owns it. Values are narrowed to
 *  what React will accept as an attribute; `false` is dropped rather than
 *  forwarded, so a boolean attribute is present only when it is actually set.
 *  hast keys stay in their own casing (`colSpan`, not `colspan`) because that
 *  is what React expects — only the allow-list comparison is lowercased. */
export const spa = (tag: string, node?: HastElement): Record<string, string | number | boolean | undefined> => {
  const out: Record<string, string | number | boolean | undefined> = sp(node)
  const allowed = TAG_ATTRS[tag]
  const props = node?.properties
  if (!allowed || !props) return out
  for (const [key, value] of Object.entries(props)) {
    const k = key.toLowerCase()
    if (k === 'classname' || k === 'class' || !allowed.has(k)) continue
    if (typeof value === 'string' || typeof value === 'number' || value === true) out[key] = value
  }
  return out
}

/** `<ol type>` → the CSS `list-style-type` it stands for. Needed because the
 *  attribute is only a presentational hint, which Tailwind's `list-style: none`
 *  preflight overrides; the marker has to be restated as a real declaration. */
const LIST_STYLE_TYPE: Record<string, string> = {
  '1': 'decimal',
  a: 'lower-alpha',
  A: 'upper-alpha',
  i: 'lower-roman',
  I: 'upper-roman',
}

/** Generate a URL-safe slug from heading children (handles nested elements) */
function textOf(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isElementWithProps(node)) {
    const props = node.props
    if (typeof props.alt === 'string') return props.alt
    if (props.children != null) return textOf(props.children)
  }
  return ''
}
/** Narrow a ReactNode to a ReactElement whose props may carry `alt`/`children`. */
export function isElementWithProps(
  node: React.ReactNode,
): node is React.ReactElement<{ alt?: string; children?: React.ReactNode }> {
  return typeof node === 'object' && node !== null && 'props' in node
}

/** The heading's id: lowercase; keep letters, numbers and combining marks of
 *  every script (the set GitHub keeps) plus `_`, whitespace and `-`; fold each
 *  whitespace run to one `-`; trim the dashes at either end. `\w` is
 *  ASCII-only in JavaScript (the `u` flag does not widen it), so the class is
 *  spelled with Unicode properties: a heading written in Japanese keeps its
 *  text as its id, and a mixed heading keeps every word instead of collapsing
 *  onto the digits it happens to contain. An ASCII heading yields the same id
 *  either way.
 *
 *  Three marks are dropped first, because they carry no text: the variation
 *  selectors (U+FE0E, U+FE0F) and the keycap enclosure (U+20E3) only turn a
 *  symbol into its emoji form, yet they are combining marks and would survive
 *  the class. `## ⚠️ Risks` keeps the id `risks` instead of gaining an
 *  invisible first character that no `#risks` link can name. */
function slugify(children: React.ReactNode): string | undefined {
  const raw = textOf(children)
    .toLowerCase()
    .replace(/[\uFE0E\uFE0F\u20E3]/g, '')
    .replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '')
    .replace(/\s+/g, '-')
    .replace(/^-+|-+$/g, '')
  return raw || undefined
}

export const ELEMENT_OVERRIDES = {
  // Headers carry the column's meaning, so never break them mid-label.
  th({ node, children }) { return <th {...spa('th', node)} className="text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated whitespace-nowrap">{children}</th> },
  td({ node, children }) { return <td {...spa('td', node)} className="px-3 py-2 border-b border-border text-sm">{children}</td> },
  blockquote({ node, children }) { return <blockquote {...sp(node)} className="border-l-[3px] border-accent pl-3 my-2 text-muted italic">{children}</blockquote> },
  hr({ node }) { return <hr {...sp(node)} className="border-border my-4" /> },
  h1({ node, children }) { const id = slugify(children); return <h1 {...sp(node)} id={id} className="text-xl font-bold mt-4 mb-2 text-text-strong">{children}</h1> },
  h2({ node, children }) { const id = slugify(children); return <h2 {...sp(node)} id={id} className="text-lg font-bold mt-3 mb-2 text-text-strong">{children}</h2> },
  h3({ node, children }) { const id = slugify(children); return <h3 {...sp(node)} id={id} className="text-base font-semibold mt-3 mb-1.5 text-text-strong">{children}</h3> },
  h4({ node, children }) { const id = slugify(children); return <h4 {...sp(node)} id={id} className="text-sm font-semibold mt-2 mb-1 text-text-strong">{children}</h4> },
  h5({ node, children }) { const id = slugify(children); return <h5 {...sp(node)} id={id} className="text-sm font-medium mt-2 mb-1 text-text-strong">{children}</h5> },
  h6({ node, children }) { const id = slugify(children); return <h6 {...sp(node)} id={id} className="text-[13px] font-medium mt-2 mb-1 text-muted">{children}</h6> },
  ul({ node, children, className }) { const isTasks = className?.includes('contains-task-list'); return <ul {...sp(node)} className={isTasks ? 'list-none pl-4 my-2 space-y-1' : 'list-disc pl-8 my-2 space-y-1 marker:text-muted'}>{children}</ul> },
  // `start` must reach the DOM, not be dropped while attaching a className: a
  // fenced block SPLITS the message into independent markdown documents
  // (useBlockAssembler), so the list after a code block is its own <ol> that
  // legitimately begins at 2, 3, … Without `start` every one of those restarts
  // at 1, which is what turned a numbered set of shell steps into four items
  // all labelled "1.". `spa` forwards it — and `type`/`reversed` — from the
  // same table the sanitizer consults.
  ol({ node, children, className }) {
    const isTasks = className?.includes('contains-task-list')
    const type = node?.properties?.type
    // Tailwind's preflight sets `ol { list-style: none }`. That is author CSS,
    // so it beats the presentational hint the `type` attribute carries — simply
    // omitting `list-decimal` for a typed list renders NO marker at all, which
    // is worse than the wrong marker. Map the attribute to an explicit
    // list-style-type instead, inline so it does not depend on Tailwind having
    // scanned an arbitrary-value class. An unrecognized type keeps the decimal
    // default.
    const styleType = typeof type === 'string' ? LIST_STYLE_TYPE[type] : undefined
    const typed = styleType != null && styleType !== 'decimal'
    return (
      <ol
        {...spa('ol', node)}
        style={typed ? { listStyleType: styleType } : undefined}
        className={isTasks ? 'list-none pl-4 my-2 space-y-1' : `${typed ? '' : 'list-decimal '}pl-8 my-2 space-y-1 marker:text-muted`}
      >
        {children}
      </ol>
    )
  },
  li({ node, children, className }) {
    const isTask = className?.includes('task-list-item')
    if (!isTask) return <li {...spa('li', node)} className="text-sm leading-relaxed">{children}</li>
    // Task items use block flow, NOT flex. The previous `flex items-start` row
    // broke two ways: (1) an item containing a NESTED list (tasks.md shape)
    // laid the child <ul> out BESIDE the text; (2) any item long enough to
    // wrap turned each inline chunk (text node / code chip) into a separate
    // flex item, so text wrapped inside one chunk while siblings floated next
    // to it — and flex min-width:auto blocked wrapping entirely, forcing
    // horizontal scroll. Block flow + hanging indent (pl/-indent pair) keeps
    // the checkbox aligned with the first line and wrapped lines under the
    // text; nested lists reset the indent and drop below.
    //
    // `text-indent` is inherited, so a LOOSE task list (blank line between
    // items) needs care: remark-rehype wraps each item's content in <p> and
    // puts the checkbox inside the FIRST <p>. The first <p> should keep the
    // hanging indent, but every subsequent <p>/block would otherwise inherit
    // the -1.25rem and jut left into the checkbox gutter — hence the
    // `[&>p:not(:first-child)]:indent-0` reset. The checkbox margin/alignment
    // uses a descendant combinator (`[&_input…]`) rather than direct-child so
    // it also lands on the loose-mode checkbox nested inside that first <p>.
    return (
      <li
        {...spa('li', node)}
        className="text-sm leading-relaxed break-words pl-5 -indent-5 [&_input[type=checkbox]]:mr-1.5 [&_input[type=checkbox]]:align-middle [&>ul]:indent-0 [&>ol]:indent-0 [&>p:not(:first-child)]:indent-0 [&>ul]:mt-1 [&>ol]:mt-1"
      >
        {children}
      </li>
    )
  },
  strong({ node, children }) { return <strong {...sp(node)} className="font-semibold text-text-strong">{children}</strong> },
  em({ node, children }) { return <em {...sp(node)} className="italic">{children}</em> },
} as Components
