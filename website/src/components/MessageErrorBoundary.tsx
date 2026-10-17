import { Component, Fragment, type ReactNode } from 'react'
import { AlertTriangle, Code } from 'lucide-react'
import AskAgentButton from './AskAgentButton'
import { recordError } from '../utils/errorReport'

import { i18nT } from '../i18n/t'
interface Props {
  children: ReactNode
  /** Raw text fallback shown when render fails */
  rawContent?: string
}

interface State {
  error: Error | null
  showRaw: boolean
  /** Bumped to force a FRESH mount of the children (see `isStaleDomError`). */
  attempt: number
  /** The `rawContent` a remount was already spent on, so the retry is one-shot
   *  per content rather than a loop. */
  retriedFor: string | null
}

/**
 * Errors where the DOM no longer matches what React's fiber tree believes, so
 * the reconciler fails while placing a node:
 *
 *   NotFoundError: Failed to execute 'insertBefore' on 'Node': The node before
 *   which the new node is to be inserted is not a child of this node.
 *   NotFoundError: Failed to execute 'removeChild' on 'Node': The node to be
 *   removed is not a child of this node.
 *
 * These say nothing about the CONTENT — the markdown is fine and renders
 * correctly from scratch. What is broken is the correspondence between the
 * existing DOM and the tree describing it, which a remount rebuilds from
 * nothing. Distinguished from an ordinary render error (a bad node type, a
 * throwing component) because those recur identically on a remount, so retrying
 * one would only cost a second crash.
 *
 * Matched on the message text rather than `error.name`: the name is
 * `NotFoundError` for both DOM cases but the same class of failure also arrives
 * as a bare `Error` from React's own invariant paths, and the quoted operation
 * is the part that is stable across browsers.
 */
export function isStaleDomError(error: Error): boolean {
  const message = error.message || ''
  return /not a child of this node/i.test(message)
    || /Failed to execute '(insertBefore|removeChild|appendChild)' on 'Node'/i.test(message)
}

/**
 * Per-message ErrorBoundary: catches render crashes (e.g. React error #290
 * from unknown HTML elements) and displays a compact fallback instead of
 * crashing the entire dashboard tree.
 *
 * Unlike the root ErrorBoundary, this is lightweight and recoverable -- the user
 * can toggle raw text view without reloading the page, a streaming message
 * recovers on its next tick, and a stale-DOM reconciliation failure recovers by
 * remounting (`isStaleDomError`).
 */
export default class MessageErrorBoundary extends Component<Props, State> {
  state: State = { error: null, showRaw: false, attempt: 0, retriedFor: null }

  static getDerivedStateFromError(error: Error) { return { error } }

  componentDidCatch(error: Error) {
    // eslint-disable-next-line no-console
    console.error('[MessageErrorBoundary] Message render failed:', error.message)
    // Journaled so the fallback's hand-off carries the stack. Deliberately NOT
    // cached on the instance: `componentDidUpdate` resets `state.error` on a
    // content change (streaming recovery) but could not reset an instance field,
    // and React runs render BEFORE componentDidCatch — so a cached report handed
    // down as a prop would be the PREVIOUS crash's. AskAgentButton looks the
    // current one up from the journal at click time instead.
    try {
      recordError({
        source: 'render',
        message: error.message || error.name,
        code: 'message_render',
        detail: [error.stack, this.props.rawContent].filter(Boolean).join('\n\n'),
      })
    } catch { /* journaling must never mask the error it describes */ }

    // A stale-DOM failure is recovered by rebuilding the subtree, ONCE per
    // content. Without this a message the user is merely LOOKING at (not
    // streaming, so `componentDidUpdate` below never fires) keeps the fallback
    // until the page is reloaded — even though its markdown renders perfectly
    // from a fresh mount. The error is still journaled above, so recovering
    // silently does not hide the defect from the error report.
    const signature = this.props.rawContent ?? ''
    if (isStaleDomError(error) && this.state.retriedFor !== signature) {
      this.setState(s => ({ error: null, showRaw: false, attempt: s.attempt + 1, retriedFor: signature }))
    }
  }

  componentDidUpdate(prevProps: Props) {
    // Streaming recovery: smoothedText updates continuously while a message
    // streams. A transient crash on an intermediate frame (e.g. partial HTML)
    // must not latch the boundary permanently — reset when the underlying
    // content changes so the next frame gets a fresh render attempt. New content
    // also earns a fresh retry budget: `retriedFor` tracked the PREVIOUS text.
    if (prevProps.rawContent !== this.props.rawContent && (this.state.error || this.state.retriedFor !== null)) {
      this.setState({ error: null, showRaw: false, retriedFor: null })
    }
  }

  render() {
    // Keyed so a bumped `attempt` discards the crashed subtree and mounts a new
    // one, which is what clears the stale DOM the reconciler tripped over.
    if (!this.state.error) return <Fragment key={this.state.attempt}>{this.props.children}</Fragment>

    return (
      <div className="flex flex-col gap-1 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle/30 text-sm">
        <div className="flex items-center gap-1.5 text-warn">
          <AlertTriangle size={14} />
          <span className="font-medium">{i18nT('components.messageErrorBoundary.message_failed_to_render')}</span>
          <AskAgentButton
            message={this.state.error.message}
            className="ml-auto"
          />
          {this.props.rawContent && (
            <button
              className="flex items-center gap-1 text-[11px] text-muted hover:text-text transition-colors cursor-pointer"
              onClick={() => this.setState(s => ({ showRaw: !s.showRaw }))}
            >
              <Code size={12} />
              {this.state.showRaw ? i18nT('components.messageErrorBoundary.hide_raw') : i18nT('components.messageErrorBoundary.view_raw')}
            </button>
          )}
        </div>
        {this.state.showRaw && this.props.rawContent && (
          <pre className="text-xs text-muted whitespace-pre-wrap break-words mt-1 max-h-[200px] overflow-y-auto font-mono bg-bg-elevated rounded p-2">
            {this.props.rawContent}
          </pre>
        )}
      </div>
    )
  }
}
