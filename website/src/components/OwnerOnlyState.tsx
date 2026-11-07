import { Link, useInRouterContext } from 'react-router-dom'
import { Lock } from 'lucide-react'
import { withBase } from '../lib/basePath'
import { i18nT } from '../i18n/t'

/** The one state a guest sees on a page only the owner can use. */
export default function OwnerOnlyState() {
  const inRouter = useInRouterContext()
  const label = i18nT('components.errorBoundary.back_to_chat')
  const className = 'text-sm text-accent hover:underline'
  return (
    <div role="status" data-testid="owner-only" className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center text-muted">
      <Lock size={20} aria-hidden />
      <div className="text-sm font-medium text-text-strong">{i18nT('components.errorBoundary.owner_only')}</div>
      {/* Outside a router (the root error boundary) a plain link reloads into the chat. */}
      {inRouter
        ? <Link to="/chat" data-testid="owner-only-back" className={className}>{label}</Link>
        : <a href={withBase('/chat')} data-testid="owner-only-back" className={className}>{label}</a>}
    </div>
  )
}
