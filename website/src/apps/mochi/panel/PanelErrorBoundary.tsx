/**
 * PanelErrorBoundary — the only crash barrier in the Mochi renderer.
 *
 * WHY: the streaming chat path re-parses partial markdown and remounts widget
 * iframes on every throttled tick; a single render throw there used to unwind
 * the whole tree and leave the panel window BLACK (empty root, no message, no
 * way out). React only stops that unwind at a class component with
 * getDerivedStateFromError / componentDidCatch — hooks cannot catch render
 * errors — so this must be a class.
 *
 * It does NOT swallow: the shell now forwards renderer console errors to the
 * main log, so we deliberately `console.error` the caught error (rather than
 * hiding it) and show a retry that re-mounts the subtree via a bumped key.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { PawPrint } from 'lucide-react'
import { i18nT } from '../../../i18n/t'
interface Props {
  children: ReactNode
}

interface State {
  /** Bumped on retry to force a fresh mount of the child subtree. */
  attempt: number
  error: Error | null
}

/** Presentational fallback — split out so the class can use the `useT` hook. */
function Fallback({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      style={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        padding: 24,
        textAlign: 'center',
        background: 'var(--bg, #16161e)',
        color: 'var(--text, #e6e6ef)',
      }}
    >
      <div style={{ color: 'var(--text-muted, #9a9aa8)' }} aria-hidden="true">
        <PawPrint size={22} />
      </div>
      <div style={{ fontWeight: 600 }}>{i18nT('apps.mochi.error.panel_title')}</div>
      <div style={{ fontSize: 12, color: 'var(--text-muted, #9a9aa8)', maxWidth: 260 }}>
        {i18nT('apps.mochi.error.panel_body')}
      </div>
      <button
        onClick={onRetry}
        style={{
          marginTop: 4,
          background: 'var(--accent, #7c6cff)',
          color: 'var(--accent-text, #fff)',
          border: 'none',
          borderRadius: 6,
          padding: '5px 14px',
          fontSize: 12,
          fontWeight: 600,
          cursor: 'pointer',
        }}
      >
        {i18nT('apps.mochi.error.panel_retry')}
      </button>
    </div>
  )
}

export class PanelErrorBoundary extends Component<Props, State> {
  state: State = { attempt: 0, error: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log, never hide: the shell tails renderer console.error into the main
    // log, so this is how a mid-stream crash becomes diagnosable.
    // eslint-disable-next-line no-console
    console.error('[mochi] panel render crashed:', error, info.componentStack)
  }

  private handleRetry = (): void => {
    // Clear the error AND bump the key so the children remount clean; a stale
    // subtree that threw would otherwise immediately throw again on re-render.
    this.setState((s) => ({ attempt: s.attempt + 1, error: null }))
  }

  render(): ReactNode {
    if (this.state.error) {
      return <Fallback onRetry={this.handleRetry} />
    }
    return <div key={this.state.attempt} style={{ display: 'contents' }}>{this.props.children}</div>
  }
}
