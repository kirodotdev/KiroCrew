import { Trans } from 'react-i18next'
import { GitMerge } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import type { MergedFromBlock } from '../../types/mergeBack'

/**
 * The line above a merge card in the parent chat: which fork the summary came
 * from and how many of its messages it covers.
 *
 * With `onOpen` the fork's title is a button that opens the fork; without it
 * (a surface with no session navigation, such as an embedded app's transcript)
 * the title is plain text. The title is the one the fork had when it was
 * merged, so a later rename does not rewrite what the card says it came from.
 */
export default function MergedFromLabel({ block, onOpen }: { block: MergedFromBlock; onOpen?: () => void }) {
  const title = block.title || i18nT('pages.chat.mergedFromLabel.untitled_fork')
  const fork = onOpen
    ? (
      // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the control's label is the fork title `Trans` renders inside it
      <button
        type="button"
        onClick={onOpen}
        className="cursor-pointer border-none bg-transparent p-0 font-medium text-accent underline-offset-2 hover:underline focus-ring rounded-sm"
        data-testid="merged-from-open"
      />
    )
    : <span className="text-text" />
  return (
    <span className="mb-1 inline-flex flex-wrap items-center gap-x-1 px-1 text-[11px] font-medium leading-4 text-muted" data-testid="merged-from-label">
      <GitMerge size={11} className="shrink-0" aria-hidden="true" />
      <span>
        <Trans i18nKey="pages.chat.mergedFromLabel.merged_from" values={{ title }} components={{ fork }} />
      </span>
      <span aria-hidden="true">·</span>
      <span>{i18nT('pages.chat.mergedFromLabel.messages', { count: block.messages })}</span>
    </span>
  )
}
