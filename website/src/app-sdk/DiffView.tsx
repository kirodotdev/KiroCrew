/**
 * The dashboard's diff viewer, offered to apps (`@kirocrew/app-sdk`).
 *
 * It is the same `components/DiffPanel` the chat window's diff tab renders:
 * side by side or unified, with the "contents identical" banner and the
 * click-to-copy path footer. It takes the two texts and sends no request, so
 * it needs no declared path and renders the same under any app identity.
 *
 * The viewer loads behind a lazy boundary: this module is part of the SDK
 * barrel the dashboard shell loads up front for every app, while the diff
 * renderer is only needed once a page shows a diff.
 */
import { lazy, Suspense } from 'react'
import { ContentSkeleton } from '../components/ui'

const HostDiffPanel = lazy(() => import('../components/DiffPanel'))

export interface AppDiffViewProps {
  /** The file's path, shown in the footer and used to pick the highlighting. */
  filePath: string
  /** The text before the change; empty for a new file. */
  original: string
  /** The text after the change; empty for a deleted file. */
  modified: string
  /** Side by side (the default) or one unified column. */
  sideBySide?: boolean
  /** Show line numbers (off by default). */
  lineNumbers?: boolean
}

export default function DiffView(props: AppDiffViewProps) {
  return (
    <Suspense fallback={<ContentSkeleton rows={6} />}>
      <HostDiffPanel {...props} />
    </Suspense>
  )
}
