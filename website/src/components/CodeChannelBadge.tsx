import { Archive, CircleCheck, ExternalLink } from 'lucide-react'
import { i18nT } from '../i18n/t'
import type { CodeChannelInfo } from '../types'
import { ChannelBrandIcon } from './ChannelBrandIcon'

/**
 * The "Slack code channel" badge for a session that IS a Slack code channel's
 * (`slot.code_channel`, set by the backend only for a bound session while code
 * channels are on, so every other session renders nothing here).
 *
 * One component, two placements: `row` is the compact chip on a sidebar row's
 * meta line (beside the agent name, so the row keeps its fixed height), `header`
 * the chip beside the session title. Both are a real anchor to the channel
 * (Slack's `app_redirect` URL, which opens the desktop app when it is installed
 * and the web client otherwise) in a new tab, marked with the external-link
 * glyph. When the backend could not build a link (a malformed record) the badge
 * still marks the session, as plain text, so it never offers a dead click.
 *
 * The accessible name always starts with "Slack code channel" (visible in the
 * header, an sr-only span on the compact row chip) so a screen reader hears what
 * the chip is before the channel's name. An archived or wrapped-up channel gets
 * its own glyph (an archive box, a check) beside the state word, so the two read
 * differently even on a meta line too narrow for the word, and the tooltip says
 * what each state means.
 */
export function codeChannelName(info: CodeChannelInfo): string {
  return info.channel_name ? `#${info.channel_name}` : ''
}

function statusLabel(status: CodeChannelInfo['status']): string {
  if (status === 'archived') return i18nT('components.codeChannelBadge.archived')
  if (status === 'wrapped_up') return i18nT('components.codeChannelBadge.wrapped_up')
  return ''
}

function statusHint(status: CodeChannelInfo['status']): string {
  if (status === 'archived') return i18nT('components.codeChannelBadge.archived_hint')
  if (status === 'wrapped_up') return i18nT('components.codeChannelBadge.wrapped_up_hint')
  return ''
}

/** Where the click goes. */
export function codeChannelOpenHint(info: CodeChannelInfo): string {
  const name = codeChannelName(info)
  return name
    ? i18nT('components.codeChannelBadge.open_named', { channel: name })
    : i18nT('components.codeChannelBadge.open_unnamed')
}

/** The tooltip: what a code channel is, what its state means, where the click goes.
 *  One sentence per line, so no locale's punctuation has to join them. */
function codeChannelTitle(info: CodeChannelInfo): string {
  return [
    i18nT('components.codeChannelBadge.about'),
    statusHint(info.status),
    info.web_url ? codeChannelOpenHint(info) : '',
  ].filter(Boolean).join('\n')
}

const CHIP_BASE =
  'inline-flex min-w-0 items-center gap-1 rounded-[4px] border no-underline transition-colors motion-reduce:transition-none'

export function CodeChannelBadge({ info, variant }: { info: CodeChannelInfo; variant: 'row' | 'header' }) {
  const name = codeChannelName(info)
  const state = statusLabel(info.status)
  const inactive = info.status !== 'active'
  const header = variant === 'header'
  const size = header ? 11 : 10
  const sizing = header
    ? 'max-w-full px-2 py-0.5 text-[11px] rounded-md pointer-events-auto'
    : 'max-w-[55%] shrink overflow-hidden px-1 py-0 text-[10px] leading-[14px] font-normal'
  const tone = inactive
    ? 'border-border bg-bg-elevated/60 text-muted'
    : 'border-border bg-accent-subtle text-text'
  const StateIcon = info.status === 'archived' ? Archive : info.status === 'wrapped_up' ? CircleCheck : null
  const body = (
    <>
      <span className="inline-flex shrink-0" aria-hidden="true">
        <ChannelBrandIcon channel="slack" size={size} />
      </span>
      {header ? (
        <span className="shrink-0 whitespace-nowrap">{i18nT('components.codeChannelBadge.label')}</span>
      ) : (
        <span className="sr-only">{i18nT('components.codeChannelBadge.label')}</span>
      )}
      {/* The row's meta line has room for one word: an active channel shows its
          name, an archived or wrapped-up one its state instead (the name stays
          in the accessible name and the tooltip). */}
      {name ? (
        <span className={`truncate min-w-0 ${header ? 'max-w-[32ch] text-muted' : state ? 'sr-only' : ''}`} dir="auto">{name}</span>
      ) : !header && !state ? (
        <span className="truncate min-w-0" aria-hidden="true">{i18nT('components.codeChannelBadge.label_short')}</span>
      ) : null}
      {StateIcon && (
        <span className="inline-flex min-w-0 items-center gap-0.5 text-muted" data-testid="code-channel-status">
          <StateIcon size={size - 1} className="shrink-0" aria-hidden="true" />
          <span className="truncate min-w-0 whitespace-nowrap">{state}</span>
        </span>
      )}
      {info.web_url && <ExternalLink size={header ? 10 : 9} className="shrink-0 text-muted" aria-hidden="true" />}
    </>
  )
  if (!info.web_url) {
    return (
      <span
        className={`${CHIP_BASE} ${sizing} ${tone}`}
        data-testid={`code-channel-badge-${variant}`}
        title={codeChannelTitle(info)}
      >
        {body}
      </span>
    )
  }
  return (
    <a
      href={info.web_url}
      target="_blank"
      rel="noopener noreferrer"
      draggable={false}
      data-testid={`code-channel-badge-${variant}`}
      // The sidebar row is a click-to-switch button and opens the SESSION in a
      // background tab on a middle click: opening the channel must do neither.
      // Pointer focus is dropped so the row's :focus-visible action strip is
      // not pinned open; Enter keeps focus.
      onClick={e => {
        e.stopPropagation()
        if (e.detail > 0) e.currentTarget.blur()
      }}
      onAuxClick={e => e.stopPropagation()}
      className={`${CHIP_BASE} ${sizing} ${tone} hover:text-text hover:border-accent focus-ring`}
      title={codeChannelTitle(info)}
    >
      {body}
      <span className="sr-only">{i18nT('components.codeChannelBadge.opens_in_slack')}</span>
    </a>
  )
}
