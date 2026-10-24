import { lazy, Suspense } from 'react'
import { ChevronDown, Copy, GitBranch } from 'lucide-react'
import type { BranchSwitcherProps } from './BranchSwitcher'
import { i18nT } from '../i18n/t'

// The picker (list, filter, keyboard model, switch flow) is ~17 KB that only
// matters once someone opens it, while both of its hosts -- the composer's
// project chip and the Git panel header -- sit in the always-loaded chat-route
// chunk. Loading it behind import() keeps that chunk inside its bundle-size
// budget (scripts/check-bundle-size.mjs).
const BranchSwitcher = lazy(() => import('./BranchSwitcher'))

/**
 * Same box as BranchSwitcher's trigger, so the chip and the header do not shift
 * when the real control replaces it. Not interactive: it is on screen only for
 * the moment the picker's chunk takes to arrive on first render.
 */
function TriggerPlaceholder({ branch, variant = 'header', switchBlocked = false }: Pick<BranchSwitcherProps, 'branch' | 'variant' | 'switchBlocked'>) {
  const Icon = switchBlocked ? Copy : ChevronDown
  if (variant === 'chip') {
    return (
      <span
        data-testid="branch-switcher-placeholder"
        className="min-w-0 max-w-[220px] inline-flex items-center gap-1 h-7 px-1.5 -mx-1 rounded-md font-mono text-inherit"
      >
        <span className="truncate">{branch}</span>
        <Icon size={11} className="shrink-0 opacity-70" />
      </span>
    )
  }
  return (
    <span
      data-testid="branch-switcher-placeholder"
      className="min-w-0 flex items-center gap-1.5 h-[26px] px-1.5 -ml-1.5 rounded-md text-text"
    >
      <GitBranch size={14} className="text-accent shrink-0" />
      <span className="text-[12px] font-medium truncate">{branch || i18nT('components.gitPanel.loading')}</span>
      <Icon size={12} className="shrink-0 text-muted" />
    </span>
  )
}

export default function LazyBranchSwitcher(props: BranchSwitcherProps) {
  return (
    <Suspense fallback={<TriggerPlaceholder branch={props.branch} variant={props.variant} switchBlocked={props.switchBlocked} />}>
      <BranchSwitcher {...props} />
    </Suspense>
  )
}
