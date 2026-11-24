import { Component, type ReactNode } from 'react'

/** A model that cannot be loaded never takes the page down with it: what is drawn in its place is `fallback`, or nothing. */
export class ModelGuard extends Component<{ children: ReactNode; fallback?: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError(): { failed: boolean } { return { failed: true } }
  render(): ReactNode { return this.state.failed ? this.props.fallback ?? null : this.props.children }
}
