import { acpBackendName, acpBackendShortName } from '../api/acpBackend'
import { i18nT } from '../i18n/t'
import type { MarkdownBlock } from '../lib/markdownAgents'

/**
 * The agent pickers' "Kiro Agent only" pill: this row's agent is defined only in
 * markdown and the harness this chat runs cannot load it.
 *
 * A compact pill with the reason on hover, like the Default pill beside it.
 * The row stays selectable on purpose: the gateway's spawn refusal still names
 * the file and the fix, and kiro-cli is the transitional harness. The pill
 * text is part of the option's accessible name, so a screen reader hears it.
 */
export function MarkdownOnlyBadge({ block }: { block?: MarkdownBlock }) {
  if (!block) return null
  const backends = block.capable.map(acpBackendShortName).join(' / ')
  const reason = i18nT('components.markdownOnlyBadge.tooltip', {
    current: acpBackendName({ id: block.backend }),
    backends,
  })
  return (
    <span
      data-testid="markdown-only-badge"
      className="shrink-0 inline-flex items-center px-1.5 py-[1px] rounded-full text-[11px] font-semibold bg-warn-subtle text-warn-text whitespace-nowrap"
      title={reason}
    >
      {i18nT('components.markdownOnlyBadge.label', { backends })}
      {/* A native title reaches neither keyboard nor screen-reader users, so the
          same reason also rides in the option's accessible text. */}
      <span className="sr-only"> {reason}</span>
    </span>
  )
}
