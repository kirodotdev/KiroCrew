import { memo } from 'react'
import { X } from 'lucide-react'
import DiffPanel from './DiffPanel'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { compareFileName, orderStates, stateLabel } from '../lib/artifactCompare'
import type { Artifact } from '../types'
import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { useIsMobile } from '../hooks/useIsMobile'
import { useDiffSplit } from '../hooks/useDiffSplit'

/**
 * Two states of one artifact, side by side with the changed lines marked.
 *
 * Both texts come from the versions API the version picker already reads, and
 * the diff itself is drawn by the viewer chat file changes use, so nothing
 * here asks the gateway for anything new. The older state sits on the left.
 */
export default memo(function ArtifactVersionCompare({
  shown, shownKey, otherKey, other, loading, error, onStop,
}: {
  /** The state the page is showing. */
  shown: Artifact
  shownKey: string
  /** The state chosen to compare it with, and its text once fetched. */
  otherKey: string
  other: Artifact | undefined
  loading: boolean
  error: unknown
  onStop: () => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // The layout follows the shared Display setting like every other diff surface. Two columns of a
  // phone's width are about 150px each, so a narrow screen gets the one-column diff either way.
  const [preferSplit] = useDiffSplit()
  const narrow = useIsMobile()
  const sideBySide = preferSplit && !narrow
  const [olderKey, newerKey] = orderStates(shownKey, otherKey)
  const older = olderKey === shownKey ? shown : other
  const newer = newerKey === shownKey ? shown : other
  return (
    <div className="flex flex-col gap-2" data-testid="artifact-version-compare">
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <span className="font-medium text-text-strong">
          {i18nT('pages.artifactDetailPage.comparing_older_with_newer', {
            older: stateLabel(olderKey), newer: stateLabel(newerKey),
          })}
        </span>
        <Btn onClick={onStop} className="ml-auto flex items-center gap-1 h-7">
          <X size={13} aria-hidden="true" /> {i18nT('pages.artifactDetailPage.stop_comparing')}
        </Btn>
      </div>
      {/* No hand-off: this page holds a live editor buffer and a comment draft that leaving for chat would discard. */}
      <ErrorNotice
        title={i18nT('pages.artifactDetailPage.compare_failed', { version: stateLabel(otherKey) })}
        message={error ? (errMessage(error) || i18nT('components.errorBoundary.something_went_wrong')) : null}
      />
      {older && newer ? (
        <div className="min-h-[24rem] border border-border rounded-md overflow-hidden">
          {sideBySide && (
            <div
              className="grid grid-cols-2 border-b border-border text-[12px] font-medium text-text-strong"
              data-testid="artifact-version-compare-captions"
            >
              <span className="px-3 py-1 truncate">{stateLabel(olderKey)}</span>
              <span className="px-3 py-1 truncate border-l border-border">{stateLabel(newerKey)}</span>
            </div>
          )}
          <DiffPanel
            filePath={compareFileName(shown)}
            original={older.content ?? ''}
            modified={newer.content ?? ''}
            sideBySide={sideBySide}
          />
        </div>
      ) : loading ? (
        <div className="text-[13px] text-muted">
          {i18nT('pages.artifactDetailPage.compare_loading', { version: stateLabel(otherKey) })}
        </div>
      ) : null}
    </div>
  )
})
