/**
 * A rendered ``<details>`` collapsible block for the Notes preview.
 *
 * The one multi-line HTML block the line-to-block renderer reads (see
 * ``parseDetailsBlock``); every other raw tag stays literal text. It renders a
 * NATIVE ``<details><summary>`` pair, so the disclosure arrives with keyboard
 * and screen-reader support for free: the browser toggles it on Enter/Space
 * when the summary is focused, and announces the expand/collapse state and the
 * expanded/collapsed role without any ARIA of our own.
 *
 * It takes already-rendered React nodes for the summary and body — ``Preview``
 * runs them through the same ``inline()`` span renderer the rest of the note
 * uses, so the content is escaped by React (no raw HTML passes through) while
 * still showing bold/italic/code/links. The block always mounts COLLAPSED: a
 * note full of long answers should open quiet, and ``<details open>`` in the
 * source is deliberately not honored (clicking the block opens its source to
 * edit, so a persisted open-state would fight the click-to-edit gesture).
 *
 * Clicking the summary toggles the disclosure and nothing else (the click is
 * stopped from reaching the editor, like the checkbox and link spans elsewhere
 * in the note); clicking the body opens the whole source to edit.
 */
import type { ReactNode } from 'react'
import { ACCENT, DOC_BODY_PX } from './constants'

export function DetailsBlock({
  summary,
  body,
}: {
  /** The ``<summary>`` content, pre-rendered, or the localized fallback label. */
  summary: ReactNode
  /** The block body, one node per source line, pre-rendered. */
  body: ReactNode
}): ReactNode {
  return (
    <details
      style={{
        border: '1px solid var(--border)',
        borderRadius: '6px',
        padding: '2px 10px',
        background: 'var(--card)',
      }}
    >
      <summary
        style={{
          cursor: 'pointer',
          fontWeight: 600,
          color: ACCENT,
          fontSize: `${DOC_BODY_PX}px`,
          // No `outline: none`: a keyboard user Tabbing onto the summary must
          // see where focus is (WCAG 2.4.7), so the app's global
          // `:focus-visible` ring (index.css) is left to apply.
        }}
        // Toggling the disclosure is the summary's own affordance; stop that
        // click from ALSO opening the click-to-edit editor on the block behind
        // it, exactly as the checkbox and link spans do. A click on the body
        // still bubbles up and opens the source to edit.
        onClick={e => e.stopPropagation()}
      >
        {summary}
      </summary>
      <div style={{ marginTop: '6px' }}>{body}</div>
    </details>
  )
}
