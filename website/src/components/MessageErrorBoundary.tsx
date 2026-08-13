import { Component, type ReactNode } from 'react'
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
}

/**
 * Per-message ErrorBoundary: catches render crashes (e.g. React error #290
 * from unknown HTML elements) and displays a compact fallback instead of
 * crashing the entire dashboard tree.
 *
 * Unlike the root ErrorBoundary, this is lightweight and recoverable --
 * the user can toggle raw text view without reloading the page.
 */
export default class MessageErrorBoundary extends Component<Props, State> {
  state: State = { error: null, showRaw: false }

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
  }

  componentDidUpdate(prevProps: Props) {
    // Streaming recovery: smoothedText updates continuously while a message
    // streams. A transient crash on an intermediate frame (e.g. partial HTML)
    // must not latch the boundary permanently — reset when the underlying
    // content changes so the next frame gets a fresh render attempt.
    if (this.state.error && prevProps.rawContent !== this.props.rawContent) {
      this.setState({ error: null, showRaw: false })
    }
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div className="flex flex-col gap-1 px-3 py-2 rounded-md border border-warning/40 bg-warning-subtle/30 text-sm">
        <div className="flex items-center gap-1.5 text-warning">
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
