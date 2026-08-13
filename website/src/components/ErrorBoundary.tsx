import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { recordEvent } from '../rum'
import AskAgentButton, { askAgentHard } from './AskAgentButton'
import { recordError } from '../utils/errorReport'

import { i18nT } from '../i18n/t'
interface State { error: Error | null }

interface Props {
  children: ReactNode
  fallback?: ReactNode
  /**
   * When true, render a full-viewport fallback with a hard "Reload page"
   * action. Use at the root of the app where a render throw would otherwise
   * unmount the entire tree and leave a blank/black screen. At the root,
   * "Try Again" alone often cannot recover (e.g. a broken provider), so a
   * reload is offered as the reliable escape hatch.
   */
  root?: boolean
  /** Optional label to attribute logged errors to a region of the app. */
  scope?: string
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  /**
   * The caught throw is journaled in `componentDidCatch` but deliberately NOT
   * cached on the instance. React runs render BEFORE `componentDidCatch`, and
   * "Try Again" clears `state.error` without being able to clear an instance
   * field — so a cached report handed down as a prop would describe the PREVIOUS
   * crash after a recover-then-recrash. `AskAgentButton` looks the current one up
   * from the journal at click time instead.
   */
  static getDerivedStateFromError(error: Error) { return { error } }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface the crash so blackouts are diagnosable — React's boundary
    // contract otherwise swallows these throws with no logging anywhere.
    // eslint-disable-next-line no-console
    console.error(`[ErrorBoundary${this.props.scope ? `:${this.props.scope}` : ''}]`, error, info.componentStack)
    try {
      recordError({
        source: 'render',
        message: error.message || error.name,
        code: this.props.scope,
        detail: [error.stack, info.componentStack].filter(Boolean).join('\n\n'),
      })
    } catch { /* journaling must never mask the error it describes */ }
    try {
      recordEvent('react_error', {
        scope: this.props.scope ?? (this.props.root ? 'root' : 'route'),
        message: error.message,
        stack: error.stack?.slice(0, 2000),
        componentStack: info.componentStack?.slice(0, 2000),
      })
    } catch {
      // RUM not initialized / unavailable — never let logging mask the error.
    }
  }

  render() {
    if (!this.state.error) return this.props.children
    // Use 'in' rather than truthiness so an explicit `fallback={null}` renders
    // nothing (the extension-slot case: a faulty contribution disappears
    // instead of showing the default error card), while an omitted fallback
    // still falls through to the default UI below.
    if ('fallback' in this.props) return this.props.fallback

    // The root fallback can render when ThemeProvider itself crashed -- before
    // the data-theme attribute and CSS variables (--text-strong, --accent, ...)
    // are applied. Theme-token classes AND Tailwind dark: utilities (gated on
    // [data-theme="dark"]) would both be inert then, leaving unreadable text on
    // a blank background -- the exact blackout this guards against. So the root
    // fallback uses explicit inline colors that depend on no theme state.
    if (this.props.root) {
      return (
        <div
          className="flex flex-col items-center justify-center gap-4 text-center p-8"
          style={{ minHeight: '100vh', width: '100%', backgroundColor: '#1a1a1a', color: '#f5f5f5' }}
        >
          <div className="text-4xl"><AlertTriangle className="lucide-inline" /></div>
          <div className="text-lg font-bold">{i18nT('components.errorBoundary.something_went_wrong')}</div>
          <div className="text-sm max-w-md break-words" style={{ color: '#b0b0b0' }}>{this.state.error.message}</div>
          <div className="flex items-center gap-2">
            <button
              className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer border-none hover:opacity-90 transition-opacity"
              style={{ backgroundColor: '#2563eb', color: '#ffffff' }}
              onClick={() => this.setState({ error: null })}>{i18nT('components.errorBoundary.try_again')}</button>
            {/* Hard navigation, not <AskAgentButton>: at the root the store or
                router may be exactly what threw, so the hand-off has to survive
                a full page load (utils/errorReport's sessionStorage channel). */}
            <button
              className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer border-none hover:opacity-90 transition-opacity"
              style={{ backgroundColor: '#33373d', color: '#f5f5f5', border: '1px solid #4a4f57' }}
              onClick={() => askAgentHard(this.state.error?.message ?? '')}>
              {i18nT('components.askAgent.ask_the_agent')}
            </button>
            <button
              className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer hover:opacity-90 transition-opacity"
              style={{ backgroundColor: '#33373d', color: '#f5f5f5', border: '1px solid #4a4f57' }}
              onClick={() => window.location.reload()}>{i18nT('components.errorBoundary.reload_page')}</button>
          </div>
        </div>
      )
    }

    // Route-level fallback renders inside ThemeProvider -- theme tokens are safe.
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 text-center p-8">
        <div className="text-4xl"><AlertTriangle className="lucide-inline" /></div>
        <div className="text-lg font-bold text-text-strong">{i18nT('components.errorBoundary.something_went_wrong')}</div>
        <div className="text-sm text-muted max-w-md break-words">{this.state.error.message}</div>
        <div className="flex items-center gap-2">
          {/* "Ask the agent" is the primary here on purpose: this is an agent app,
              and after a crash the agent is likelier to resolve it than a retry. */}
          <AskAgentButton
            message={this.state.error.message}
            variant="solid"
            hard
          />
          <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-transparent text-muted border border-border hover:text-text hover:border-text-strong transition-colors"
            onClick={() => this.setState({ error: null })}>{i18nT('components.errorBoundary.try_again')}</button>
        </div>
      </div>
    )
  }
}
