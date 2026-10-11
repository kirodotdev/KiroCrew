/**
 * The parse-side remark plugins every Markdown-to-HTML path in the chat shares:
 * the transcript renderer and the rich-text copy. One list, so the copy parses
 * a reply exactly the way the transcript does.
 *
 * ORDER IS LOAD-BEARING. `remarkBoundDepth` first, because it bounds the
 * parsed tree's depth ahead of remark-gfm's recursive post-parse transform.
 * `remark-cjk-friendly` before remark-gfm, because it changes how emphasis
 * delimiters are classified, and its strikethrough companion after, because
 * it extends gfm's own `~~` construct.
 */
import remarkGfm from 'remark-gfm'
import remarkCjkFriendly from 'remark-cjk-friendly'
import remarkCjkFriendlyGfmStrikethrough from 'remark-cjk-friendly-gfm-strikethrough'
import type { PluggableList } from 'unified'
import { remarkBoundDepth } from '../../utils/markdownDepthBound'

export const REMARK_BASE_PLUGINS: PluggableList = [
  remarkBoundDepth,
  remarkCjkFriendly,
  remarkGfm,
  remarkCjkFriendlyGfmStrikethrough,
]
