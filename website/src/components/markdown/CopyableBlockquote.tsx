import { createContext, useContext, useState } from 'react'
import type { Element as HastElement } from 'hast'
import type {
  Blockquote as MdastBlockquote,
  Definition as MdastDefinition,
  FootnoteDefinition as MdastFootnoteDefinition,
  Nodes as MdastNodes,
  Parent as MdastParent,
  Root as MdastRoot,
  Text as MdastText,
} from 'mdast'
import { Check, Copy } from 'lucide-react'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import remarkStringify from 'remark-stringify'
import { unified, type PluggableList } from 'unified'
import ErrorNotice from '../ErrorNotice'
import { copyRichToClipboard } from '../../utils/clipboard'
import { HOVER_NONE_ACTION_BTN_CLS } from '../../utils/touchActions'
import { i18nT } from '../../i18n/t'
import { markdownToCleanHtml } from './richTextClipboard'
import { MdSourceCtx } from './contexts'
import { REMARK_BASE_PLUGINS } from './remarkBase'
import { ALLOWED_TAGS, VERBATIM_CONTENT_TAGS } from './sanitize'
import { remarkLatexDelimiters } from '../../utils/remarkLatexDelimiters'

// Only the outermost quote gets a button; a nested one is copied with it.
const InsideBlockquote = createContext(false)
// Math plugins as in MarkdownRenderer, so quoted math stringifies back as math.
const MATH_PLUGINS: PluggableList = [
  [remarkMath, { singleDollarTextMath: false }],
  [remarkLatexDelimiters, { verbatimTag: (tag: string) => VERBATIM_CONTENT_TAGS.has(tag) || !ALLOWED_TAGS.has(tag) }],
]
const sourceProcessor = unified().use(remarkParse).use(REMARK_BASE_PLUGINS).use(MATH_PLUGINS).use(remarkStringify)
// Same output with text written as read, without escapes, for the plain-text flavour.
const plainProcessor = unified()
  .use(remarkParse)
  .use(REMARK_BASE_PLUGINS)
  .use(MATH_PLUGINS)
  .use(remarkStringify, { handlers: { text: (node: MdastText) => node.value } })

function walk(tree: MdastRoot, visit: (node: MdastNodes) => boolean | void): void {
  const stack: MdastNodes[] = [...tree.children].reverse()
  while (stack.length) {
    const current = stack.pop()!
    if (visit(current)) return
    if ('children' in current) stack.push(...[...current.children].reverse())
  }
}

function rewriteReferences(
  parent: MdastParent,
  definitions: ReadonlyMap<string, MdastDefinition>,
  footnotes: Set<string>,
): void {
  const children = parent.children as MdastNodes[]
  for (let i = 0; i < children.length;) {
    const node = children[i]
    if (node.type === 'definition' || node.type === 'footnoteDefinition') {
      children.splice(i, 1)
      continue
    }
    if (node.type === 'linkReference') {
      const definition = definitions.get(node.identifier)
      if (definition) {
        children[i] = {
          type: 'link',
          url: definition.url,
          title: definition.title,
          children: node.children,
        }
      }
    } else if (node.type === 'imageReference') {
      const definition = definitions.get(node.identifier)
      if (definition) {
        children[i] = {
          type: 'image',
          url: definition.url,
          title: definition.title,
          alt: node.alt,
        }
      }
    } else if (node.type === 'footnoteReference') {
      footnotes.add(node.identifier)
    }
    const rewritten = children[i]
    if ('children' in rewritten) rewriteReferences(rewritten as MdastParent, definitions, footnotes)
    i++
  }
}

// Stringify writes `\( … \)` math as `$$`; keep the author's single-line source instead.
function keepLatexInlineMath(parent: MdastParent, source: string): void {
  const children = parent.children as MdastNodes[]
  children.forEach((node, i) => {
    if (node.type === 'inlineMath') {
      const raw = source.slice(node.position?.start.offset ?? 0, node.position?.end.offset ?? 0)
      if (raw.startsWith('\\(') && !raw.includes('\n')) children[i] = { type: 'html', value: raw }
    } else if ('children' in node) keepLatexInlineMath(node as MdastParent, source)
  })
}

function quoteInnerMarkdown(
  source: string | null,
  node: HastElement | undefined,
): { markdown: string; plain: string } | null {
  const start = node?.position?.start?.offset
  const end = node?.position?.end?.offset
  if (source == null || start == null || end == null) return null

  const tree = sourceProcessor.runSync(sourceProcessor.parse(source), source) as MdastRoot
  const definitions = new Map<string, MdastDefinition>()
  const footnoteDefinitions = new Map<string, MdastFootnoteDefinition>()
  let quote: MdastBlockquote | undefined
  walk(tree, current => {
    if (current.type === 'definition' && !definitions.has(current.identifier)) definitions.set(current.identifier, current)
    if (current.type === 'footnoteDefinition' && !footnoteDefinitions.has(current.identifier)) {
      footnoteDefinitions.set(current.identifier, current)
    }
    if (current.type === 'blockquote' && current.position?.start.offset === start && current.position.end.offset === end) quote = current
  })
  if (!quote) return null

  const fragment: MdastRoot = { type: 'root', children: structuredClone(quote.children) }
  const pendingFootnotes = new Set<string>()
  rewriteReferences(fragment, definitions, pendingFootnotes)
  const includedFootnotes = new Set<string>()
  for (const identifier of pendingFootnotes) {
    if (includedFootnotes.has(identifier)) continue
    includedFootnotes.add(identifier)
    const definition = footnoteDefinitions.get(identifier)
    if (!definition) continue
    const copy = structuredClone(definition)
    rewriteReferences(copy, definitions, pendingFootnotes)
    fragment.children.push(copy)
  }
  keepLatexInlineMath(fragment, source)

  return { markdown: sourceProcessor.stringify(fragment).trim(), plain: plainProcessor.stringify(fragment).trim() }
}

/** A blockquote with a hover Copy button that copies its contents as rich
 *  text, so a drafted message pastes into Slack or a ticket with live links. */
export function CopyableBlockquote({ node, children }: { node?: HastElement; children?: React.ReactNode }) {
  const nested = useContext(InsideBlockquote)
  const source = useContext(MdSourceCtx)
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  // A raw-HTML `<blockquote>` has no `>` markdown source to copy, so it gets no button.
  const startOffset = node?.position?.start?.offset
  const copyable = !nested && source != null && startOffset != null && source.charAt(startOffset) === '>'

  const copy = async () => {
    const quoted = quoteInnerMarkdown(source, node)
    setCopied(false)
    setCopyFailed(false)
    if (quoted == null) {
      setCopyFailed(true)
      return
    }
    // Same conversion as the message's "Copy as rich text", unescaped Markdown as the plain flavour.
    if (!(await copyRichToClipboard(markdownToCleanHtml(quoted.markdown), quoted.plain))) {
      setCopyFailed(true)
      return
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const sourcepos = node?.properties?.['data-sourcepos']
  const label = copied ? i18nT('components.blockquote.copied') : i18nT('components.blockquote.copy')
  return (
    <InsideBlockquote.Provider value={nested || copyable}>
      <blockquote data-sourcepos={typeof sourcepos === 'string' ? sourcepos : undefined} className={`border-l-[3px] border-accent pl-3 my-2 text-muted italic${copyable ? ' group/quote relative pr-7' : ''}`}>
        {children}
        {copyable && copyFailed && (
          <>
            {/* No hand-off: navigation could discard an unsent draft; a refused write is recoverable by selecting the quote. */}
            <ErrorNotice
              variant="inline"
              className="mt-2 not-italic"
              message={i18nT('components.blockquote.copy_failed')}
              onDismiss={() => setCopyFailed(false)}
            />
          </>
        )}
        {copyable && (
          <button
            type="button"
            data-testid="blockquote-copy"
            className={`absolute top-0 right-0 p-1 rounded not-italic text-muted hover:text-text hover:bg-bg-hover cursor-pointer opacity-0 group-hover/quote:opacity-100 focus-visible:opacity-100 transition-opacity ${HOVER_NONE_ACTION_BTN_CLS}`}
            onClick={copy}
            title={label}
            aria-label={label}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        )}
      </blockquote>
    </InsideBlockquote.Provider>
  )
}
