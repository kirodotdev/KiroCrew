import { Fragment, type ReactNode } from 'react'
import { isDiffText } from '../utils/diffUtils'
import { patchLineKinds, type PatchLineKind } from '../utils/diffLineCounts'

/** Row styles from the theme's diff tokens, the ones the transcript's diff
 *  cards paint with, keyed by the shared patch walk's line kinds. Added and
 *  removed rows also keep their leading `+` / `-` and carry a left bar, so the
 *  colour is never the only cue. Inline spans with cloned decoration, so a
 *  wrapped long line keeps its colour on every row. */
const HEADER = 'font-semibold text-[var(--diff-meta-text)]'
const ROW: Record<PatchLineKind, string> = {
  'file': HEADER,
  'old-header': HEADER,
  'new-header': HEADER,
  'meta': HEADER,
  'hunk': 'bg-[var(--diff-hunk)] text-[var(--diff-hunk-text)] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]',
  'add': 'bg-[var(--diff-add)] text-[var(--diff-add-text)] border-l-2 border-[var(--diff-add-text)] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]',
  'del': 'bg-[var(--diff-del)] text-[var(--diff-del-text)] border-l-2 border-[var(--diff-del-text)] [box-decoration-break:clone] [-webkit-box-decoration-break:clone]',
  'ctx': '',
  'marker': 'text-muted',
}

/** An approval card's tool input, for the card's own `<pre>`.
 *
 *  Approve authorizes the whole input, so every character is shown verbatim and
 *  in order: the element's text is exactly `text`. A unified diff only gains
 *  per-line styling (file headers, hunk lines, added and removed lines); nothing
 *  is parsed away, folded or turned into a link or a control. Anything else is
 *  the plain text. */
export function ApprovalInputText({ text }: { text: string }): ReactNode {
  if (!isDiffText(text)) return text
  const lines = text.split('\n')
  const kinds = patchLineKinds(lines)
  return lines.map((line, i) => (
    <Fragment key={i}>
      <span data-diff-line={kinds[i]} className={ROW[kinds[i]]}>{line}</span>
      {i < lines.length - 1 ? '\n' : null}
    </Fragment>
  ))
}
