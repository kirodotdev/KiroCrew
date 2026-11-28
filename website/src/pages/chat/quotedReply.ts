import type { Nodes as MdastNodes, Root as MdastRoot } from 'mdast'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified, type PluggableList } from 'unified'
import { REMARK_BASE_PLUGINS } from '../../components/markdown/remarkBase'
import { ALLOWED_TAGS, VERBATIM_CONTENT_TAGS } from '../../components/markdown/sanitize'
import type { ChatMessage } from '../../types'
import { capWhitespaceRuns } from '../../utils/markdownDepthBound'
import { remarkLatexDelimiters } from '../../utils/remarkLatexDelimiters'

/**
 * Where a selection quote came from. Quote on selected reply text puts the words
 * into the draft as `> ` lines (`chat-core/composer/quoteDraft.ts`), which name
 * no message, so the sent quote finds its source by its words: the nearest
 * earlier reply holding them whole, else, for a selection cut mid-word, the
 * nearest holding their letters and digits. The quote is text the browser
 * rendered, so the reply is compared as rendered text, read from its parsed
 * markdown: emphasis, line breaks and list numbering cannot stop a match.
 */

const NOT_A_WORD_CHAR = /[^\p{L}\p{N}]+/gu

/** `text`'s words, one space apart. */
function wordsOf(text: string): string {
  return text.replace(NOT_A_WORD_CHAR, ' ').trim()
}

// Math plugins as in MarkdownRenderer, so a formula's TeX is not read as words.
const MATH_PLUGINS: PluggableList = [
  [remarkMath, { singleDollarTextMath: false }],
  [remarkLatexDelimiters, { verbatimTag: (tag: string) => VERBATIM_CONTENT_TAGS.has(tag) || !ALLOWED_TAGS.has(tag) }],
]
const parser = unified().use(remarkParse).use(REMARK_BASE_PLUGINS).use(MATH_PLUGINS)

/** The text a markdown node renders, a space between its children: raw HTML,
 *  image alts, link definitions and math (drawn as a formula, not its TeX) draw
 *  none. */
function renderedText(node: MdastNodes): string {
  if (node.type === 'text' || node.type === 'inlineCode' || node.type === 'code') return node.value
  return 'children' in node ? (node.children as MdastNodes[]).map(renderedText).join(' ') : ''
}

/** Per row object, which the store keeps by reference while it is unchanged. */
const replyWordsCache = new WeakMap<ChatMessage, { words: string; letters: string }>()

function replyWords(m: ChatMessage) {
  let cached = replyWordsCache.get(m)
  if (cached === undefined) {
    const text = capWhitespaceRuns(m.content)
    // The source goes in as the file: `remarkLatexDelimiters` reads `\( … \)` from it.
    const words = wordsOf(renderedText(parser.runSync(parser.parse(text), text) as MdastRoot))
    cached = { words: ` ${words} `, letters: words.replaceAll(' ', '') }
    replyWordsCache.set(m, cached)
  }
  return cached
}

/** The reply a selection quote sent in `messages[fromIdx]` was taken from, or null. */
export function findQuotedReply(messages: ChatMessage[], quoted: string, fromIdx: number): ChatMessage | null {
  const words = wordsOf(quoted)
  if (!words) return null
  for (const pass of ['words', 'letters'] as const) {
    const needle = pass === 'words' ? ` ${words} ` : words.replaceAll(' ', '')
    for (let i = fromIdx - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.role === 'assistant' && m.ts && replyWords(m)[pass].includes(needle)) return m
    }
  }
  return null
}

/** The span of `quoted`'s words in `root`'s text, whole words first, across
 *  element boundaries, or null. A text node's edge splits words, as a node's does
 *  in `renderedText`. Chrome marked `data-message-search-skip` is not message
 *  text. */
export function quotedTextRange(root: HTMLElement, quoted: string): Range | null {
  const words = wordsOf(quoted)
  if (!words) return null
  let text = ''
  const at: Array<[Text, number]> = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.parentElement?.closest('[data-message-search-skip]') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  })
  for (let n = walker.nextNode() as Text | null; n; n = walker.nextNode() as Text | null) {
    for (const m of n.data.matchAll(/[\p{L}\p{N}]+/gu)) {
      // The space before a word maps to its first character; no match starts or ends on it.
      text += ` ${m[0]}`
      at.push([n, m.index])
      for (let k = 0; k < m[0].length; k++) at.push([n, m.index + k])
    }
  }
  let start = `${text} `.indexOf(` ${words} `) + 1
  let end = start + words.length - 1
  if (start === 0) {
    const letters = words.replaceAll(' ', '')
    const found = text.replaceAll(' ', '').indexOf(letters)
    if (found < 0) return null
    const letterAt: number[] = []
    for (let i = 0; i < text.length; i++) if (text[i] !== ' ') letterAt.push(i)
    start = letterAt[found]
    end = letterAt[found + letters.length - 1]
  }
  const [first, firstOffset] = at[start]
  const [last, lastOffset] = at[end]
  const range = document.createRange()
  range.setStart(first, firstOffset)
  range.setEnd(last, lastOffset + 1)
  return range
}

/**
 * The ref of the row a quote jump lands on: marks the quote's words in the
 * row's bubble while it is attached, and unmarks them when React detaches it.
 * The mark is a CSS Custom Highlight (`::highlight(mc-quote-mark)`), so no node
 * React owns is split. Where the API is absent, or the words are not in the
 * bubble's text, nothing is marked and the row's ring alone shows the landing.
 */
export function quoteMarkRef(quoted: string): (el: HTMLElement | null) => void {
  return el => {
    if (typeof Highlight === 'undefined' || typeof CSS === 'undefined' || !CSS.highlights) return
    const range = el && quotedTextRange(el.querySelector<HTMLElement>('.message-bubble') ?? el, quoted)
    if (range) CSS.highlights.set('mc-quote-mark', new Highlight(range))
    else CSS.highlights.delete('mc-quote-mark')
  }
}
