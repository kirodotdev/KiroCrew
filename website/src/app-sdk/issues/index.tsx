/**
 * The issue components offered on `@kirocrew/app-sdk`.
 *
 * `useIssues` is small and lives on the barrel directly. The two components load
 * behind a lazy boundary: the barrel is in the dashboard shell every app loads,
 * and the views are needed only once a page shows an issue.
 */
import { lazy, Suspense } from 'react'
import { Skeleton } from '../../components/ui'
import type { IssueCardProps } from './IssueCard'
import type { IssueListProps } from './IssueList'

const LazyIssueList = lazy(() => import('./IssueList'))
const LazyIssueCard = lazy(() => import('./IssueCard'))

export function IssueList(props: IssueListProps) {
  return (
    <Suspense fallback={<Skeleton className="h-7 w-full" />}>
      <LazyIssueList {...props} />
    </Suspense>
  )
}

export function IssueCard(props: IssueCardProps) {
  return (
    <Suspense fallback={<Skeleton className={props.view === 'expanded' ? 'h-28 w-full rounded-lg' : 'h-7 w-full'} />}>
      <LazyIssueCard {...props} />
    </Suspense>
  )
}

export { useIssues } from './useIssues'
export type { UseIssuesOptions, UseIssuesResult } from './useIssues'
export type { IssueCardProps, IssueView } from './IssueCard'
export type { IssueListProps } from './IssueList'
