import { Archive, CircleCheck, Code, ExternalLink } from 'lucide-react'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'
import type { ChatSlot, CodeChannelInfo } from '../types'
import { ChannelBrandIcon } from './ChannelBrandIcon'

/**
 * The marks a Slack-linked session wears in the dashboard.
 *
 * A session that IS a Slack code channel's carries `slot.code_channel` (set by
 * the backend only for a bound session while code channels are on, so every
 * other session renders nothing here). Two placements:
 *
 *   - `row`: on a sidebar row's meta line, the Slack mark ALONE. It is a link
 *     to the channel; its accessible name and tooltip ("Open #name in Slack")
 *     carry the channel, so no channel text competes with the agent name and
 *     the time for the row's width. A fixed box that never shrinks.
 *   - `header`: beside the session title, the full link: the Slack mark,
 *     "#name" (truncated, the whole name in the tooltip), any state (archived,
 *     task done) and the external-link glyph.
 *
 * A session mirrored to a Slack thread gets the same header link to its
 * thread (`SlackThreadLink`). Every link is Slack's own URL (the backend's
 * `app_redirect` URL for a channel, a `slack.com/archives` permalink for a
 * thread): it opens Slack and carries no dashboard credential. When no
 * well-formed URL exists the mark still shows, as plain text, so it never
 * offers a dead click.
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

/** Where the click goes: "Open #name in Slack". */
export function codeChannelOpenHint(info: CodeChannelInfo): string {
  const name = codeChannelName(info)
  return name
    ? i18nT('components.codeChannelBadge.open_named', { channel: name })
    : i18nT('components.codeChannelBadge.open_unnamed')
}

/** The backend's `app_redirect` URL, or "" for anything else. The server
 *  already validates it; checked here too so no path that forwards a slot row
 *  unvalidated can put another scheme or host into an `href`. */
const SLACK_APP_REDIRECT = /^https:\/\/slack\.com\/app_redirect\?channel=[CGD][A-Z0-9]{2,30}&team=[TE][A-Z0-9]{2,30}$/
export function codeChannelUrl(info: CodeChannelInfo): string {
  return SLACK_APP_REDIRECT.test(info.web_url ?? '') ? info.web_url : ''
}

/** One sentence per line, so no locale's punctuation has to join them. */
function lines(...parts: string[]): string {
  return parts.filter(Boolean).join('\n')
}

/** Click handling every Slack link here shares (`target`/`rel` sit on each
 *  anchor). */
const LINK_HANDLERS = {
  draggable: false,
  // A sidebar row is a click-to-switch button and opens the SESSION in a
  // background tab on a middle click: opening Slack must do neither. Pointer
  // focus is dropped so the row's :focus-visible action strip is not pinned
  // open; Enter keeps focus.
  onClick: (e: React.MouseEvent<HTMLAnchorElement>) => {
    e.stopPropagation()
    if (e.detail > 0) e.currentTarget.blur()
  },
  onAuxClick: (e: React.MouseEvent<HTMLAnchorElement>) => e.stopPropagation(),
} as const

/** The Slack mark in a fixed box that never shrinks. */
function SlackMark({ size, dim = false }: { size: number; dim?: boolean }) {
  return (
    <span
      className={`inline-flex shrink-0 ${dim ? 'opacity-50 grayscale' : ''}`}
      style={{ width: size, height: size }}
      aria-hidden="true"
      data-testid="code-channel-slack-mark"
    >
      <ChannelBrandIcon channel="slack" size={size} />
    </span>
  )
}

/** The phone top bar's marker: the Slack mark with a code glyph, so it cannot
 *  be taken for the plain Slack mark an ordinary Slack-linked session wears.
 *  A marker, not a link: the bar's title is already the session menu's
 *  trigger, and the sidebar row carries the link. */
export function CodeChannelMark({ size = 12 }: { size?: number }) {
  const label = i18nT('components.codeChannelBadge.label')
  return (
    <span className="inline-flex shrink-0 items-center gap-px" role="img" aria-label={label} title={label} data-testid="mobile-code-channel-glyph">
      <ChannelBrandIcon channel="slack" size={size} />
      <Code size={size - 2} className="shrink-0 text-muted" aria-hidden="true" />
    </span>
  )
}

/** The sidebar row's mark: the Slack mark alone, a link named for its channel.
 *  An archived channel's mark is greyed; its state is in the tooltip and the
 *  accessible description, and in words on the session header. */
function RowMark({ info }: { info: CodeChannelInfo }) {
  const dim = info.status === 'archived'
  const testProps = { 'data-testid': 'code-channel-badge-row', 'data-code-channel-status': info.status }
  const url = codeChannelUrl(info)
  if (!url) {
    const label = i18nT('components.codeChannelBadge.label')
    return (
      <span role="img" aria-label={label} title={lines(label, statusHint(info.status))} className="inline-flex shrink-0" {...testProps}>
        <SlackMark size={10} dim={dim} />
      </span>
    )
  }
  const open = codeChannelOpenHint(info)
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      {...LINK_HANDLERS}
      {...testProps}
      aria-label={open}
      title={lines(open, statusHint(info.status))}
      className="inline-flex shrink-0 items-center justify-center rounded-sm hover:opacity-80 transition-opacity motion-reduce:transition-none focus-ring"
    >
      <SlackMark size={10} dim={dim} />
    </a>
  )
}

const HEADER_CHIP =
  'inline-flex min-w-0 max-w-full shrink items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] no-underline pointer-events-auto transition-colors motion-reduce:transition-none'

/** A header chip: the Slack mark, the visible text, then the link glyph. The
 *  same chip pattern as the header's other chips (`ChatHeaderChips`). */
function HeaderChip({ href, title, testId, tone, children }: {
  href: string; title: string; testId: string; tone: string; children: ReactNode
}) {
  const body = (
    <>
      <SlackMark size={11} />
      {children}
    </>
  )
  if (!href) {
    return <span className={`${HEADER_CHIP} ${tone}`} title={title} data-testid={testId}>{body}</span>
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" {...LINK_HANDLERS} data-testid={testId} title={title}
      className={`${HEADER_CHIP} ${tone} hover:text-text hover:border-accent focus-ring`}>
      {body}
      <ExternalLink size={10} className="shrink-0 text-muted" aria-hidden="true" data-testid="code-channel-link-glyph" />
      <span className="sr-only">{i18nT('components.codeChannelBadge.opens_in_slack')}</span>
    </a>
  )
}

/** The session header's full link to the code channel. */
function HeaderLink({ info }: { info: CodeChannelInfo }) {
  const name = codeChannelName(info)
  const state = statusLabel(info.status)
  const StateIcon = info.status === 'archived' ? Archive : info.status === 'wrapped_up' ? CircleCheck : null
  const stateTone = info.status === 'wrapped_up' ? 'text-ok' : 'text-muted'
  const tone = info.status === 'archived'
    ? 'border-border border-dashed bg-bg-elevated/60 text-muted'
    : 'border-border bg-accent-subtle text-text'
  const url = codeChannelUrl(info)
  return (
    <HeaderChip
      href={url}
      testId="code-channel-badge-header"
      tone={tone}
      title={lines(
        i18nT('components.codeChannelBadge.about'),
        statusHint(info.status),
        url ? codeChannelOpenHint(info) : '',
      )}
    >
      {/* Read first, so a screen reader hears what the chip is before its name. */}
      <span className="sr-only">{i18nT('components.codeChannelBadge.label')}</span>
      {name
        ? <span className="truncate min-w-0 max-w-[32ch]" dir="auto" data-testid="code-channel-name">{name}</span>
        : <span className="shrink-0 whitespace-nowrap">{i18nT('components.codeChannelBadge.label_short')}</span>}
      {StateIcon && (
        <span className={`inline-flex min-w-0 items-center gap-0.5 ${stateTone}`} data-testid="code-channel-status">
          <StateIcon size={10} className="shrink-0" aria-hidden="true" />
          <span className="truncate min-w-0 whitespace-nowrap">{state}</span>
        </span>
      )}
    </HeaderChip>
  )
}

export function CodeChannelBadge({ info, variant }: { info: CodeChannelInfo; variant: 'row' | 'header' }) {
  return variant === 'row' ? <RowMark info={info} /> : <HeaderLink info={info} />
}

/** The flex weight a code-channel row's agent name gets: it yields before
 *  anything else on the meta line, so the Slack mark and the time keep their
 *  room. Every other row keeps its agent name `shrink-0`. */
export const CODE_CHANNEL_AGENT_FIT = 'min-w-0 shrink-[1000]'

/** Slack conversation ids and message timestamps, as the slot list carries them. */
const SLACK_CHANNEL_ID = /^[CGD][A-Z0-9]{2,30}$/
// Slack message timestamps: ten digits of seconds and six of sequence. Any
// other shape would make a permalink to the wrong message.
const SLACK_TS = /^\d{10}\.\d{6}$/

/** The permalink of a mirrored Slack thread, or "" when the ids are not
 *  well-formed (so nothing attacker-shaped can reach an `href`). */
export function slackThreadUrl(channel: string | undefined, threadTs: string | undefined): string {
  const c = (channel ?? '').trim()
  const ts = (threadTs ?? '').trim()
  if (!SLACK_CHANNEL_ID.test(c) || !SLACK_TS.test(ts)) return ''
  return `https://slack.com/archives/${c}/p${ts.replace('.', '')}`
}

/** The session header's link to the Slack thread a session is mirrored to.
 *  Renders nothing for a session with no Slack thread, nothing for a code
 *  channel's session (its own header link covers it), and nothing while the
 *  gateway cannot yet tell whether the session is a code channel's
 *  (`code_channel_pending`), so a bound session never shows this link and then
 *  swaps it for its own. Slots carry the thread's channel ID but not its name,
 *  so the chip says "Slack thread" rather than "#name". A paused mirror says so.
 *
 *  A session STARTED in Slack is not `slack_linked` (its thread is where it
 *  lives, not a mirror), so its thread comes from `slack_origin_channel` /
 *  `slack_origin_thread_ts` instead, and its paused state from the `origin`
 *  Slack row rather than an outbound one. */
export function SlackThreadLink({ slot }: { slot?: Pick<ChatSlot, 'code_channel' | 'code_channel_pending' | 'slack_linked' | 'slack_channel' | 'slack_thread_ts' | 'slack_origin_channel' | 'slack_origin_thread_ts' | 'links'> | null }) {
  if (!slot || slot.code_channel || slot.code_channel_pending) return null
  const mirrored = !!slot.slack_linked
  const href = mirrored
    ? slackThreadUrl(slot.slack_channel, slot.slack_thread_ts)
    : slackThreadUrl(slot.slack_origin_channel, slot.slack_origin_thread_ts)
  if (!href) return null
  const paused = !!slot.links?.some(link =>
    link.channel === 'slack' && (mirrored ? link.direction !== 'origin' : link.direction === 'origin') && link.paused)
  return (
    <HeaderChip
      href={href}
      testId="slack-thread-link-header"
      tone={paused ? 'border-border border-dashed bg-bg-elevated/60 text-muted' : 'border-border bg-accent-subtle text-text'}
      title={lines(i18nT('components.codeChannelBadge.thread_open'), paused ? i18nT('components.codeChannelBadge.thread_paused_hint') : '')}
    >
      <span className="truncate min-w-0 whitespace-nowrap">{i18nT('components.codeChannelBadge.thread_label')}</span>
      {paused && (
        <span className="truncate min-w-0 whitespace-nowrap text-muted" data-testid="slack-thread-paused">
          {i18nT('components.codeChannelBadge.thread_paused')}
        </span>
      )}
    </HeaderChip>
  )
}
