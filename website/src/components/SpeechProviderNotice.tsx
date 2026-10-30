import { Info } from 'lucide-react'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'
import type { SpeechProviderEntry } from '../lib/speechProviders'

/**
 * Why the selected speech provider cannot run, under its selector.
 *
 * Two kinds of row reach here. A provider whose check timed out or raised
 * (`error`) is a failure, so it renders through `ErrorNotice`, with the hand-off
 * when the caller says nothing on screen would be lost. A provider that is
 * merely waiting on a prerequisite (a login, an install) has not failed: that is
 * status text, and the provider stays selectable either way. `reason` and the
 * account label are the provider's own text, so they render as given; the
 * sentence around the account goes through the catalog.
 */
export default function SpeechProviderNotice({
  entry,
  testId,
  askAgent,
}: {
  entry: SpeechProviderEntry
  testId: string
  /** Whether the agent hand-off may show; off while the panel holds an unsaved draft. */
  askAgent: boolean
}) {
  const reason = entry.reason ?? i18nT('lib.speechProviders.unavailable_no_reason')
  if (entry.error) {
    return <ErrorNotice variant="inline" className="-mt-1 mb-3" message={reason} askAgent={askAgent} testId={testId} />
  }
  return (
    <div role="status" className="-mt-1 mb-3 flex flex-col gap-1 text-[13px]" data-testid={testId}>
      <span className="flex items-center gap-1.5">
        <Info size={13} className="lucide-inline" />
        {reason}
      </span>
      {entry.action?.kind === 'login' && (
        <span className="text-muted" data-testid={`${testId}-login`}>
          {i18nT('lib.speechProviders.sign_in_hint', { account: entry.action.label })}
        </span>
      )}
    </div>
  )
}
