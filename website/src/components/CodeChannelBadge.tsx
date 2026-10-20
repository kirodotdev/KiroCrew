import { Archive, CircleCheck, Code, ExternalLink } from 'lucide-react'
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
  'inline-flex items-center gap-1 rounded-[4px] border no-underline transition-colors motion-reduce:transition-none'

/**
 * How the row chip gives up width on a short meta line (agent name, this chip,
 * the time), in this order:
 *
 *   1. the agent name yields first (`CODE_CHANNEL_AGENT_FIT`, in `SessionRow`);
 *   2. then the external-link glyph leaves, before the word loses a character;
 *   3. then the word (the channel name, or the state) truncates, down to a
 *      readable floor of "#" plus a few characters;
 *   4. below that floor the chip goes icon-only: the Slack mark alone, the name
 *      in the tooltip and the accessible name. Never a bare "#…".
 *
 * The Slack mark and the time never shrink.
 *
 * Two nested wrapping lines, each clipped to one line by a fixed height, do it
 * in CSS alone. The chip holds the mark and a text slot (`ROW_TEXT_FIT`) that
 * has no width of its own (`basis-0 grow`) but a floor; a chip too narrow for
 * mark plus floor wraps the slot onto its hidden second line (step 4). Inside
 * the slot the word sits at its full width, so a word and glyph that do not fit
 * side by side wrap the glyph away first (step 2), and the word, alone on its
 * line, then truncates (step 3).
 *
 * Under 10rem of meta line (a phone) the padding and gaps tighten; under 8.5rem
 * (the sidebar at its narrowest) the padding drops to 1px. Those
 * container-query classes sit inline on the chip (the strict i18n gate reads an
 * ALL-CAPS constant holding `@` as copy).
 *
 * An icon-only chip can be wider than its mark (the meta line hands it
 * whatever is left), so its one line is centred: a pill with the mark in the
 * middle, never a mark with an empty tail. There is no cap on the chip's share
 * of the line, because a cap would cut the channel name while the agent name
 * still had width to give.
 */
const ROW_CHIP_FIT =
  'h-4 shrink flex-wrap content-start justify-center overflow-hidden px-1'
// The word's floor: "#" plus a few characters of a channel name. A state word
// sits beside its own glyph, which already says what changed, so its floor is
// a little lower: "Chan…" still reads beside the archive box, where wrapping the
// word away would leave a near-empty chip at an ordinary width.
const ROW_TEXT_FIT =
  'basis-0 grow h-[14px] inline-flex flex-wrap content-start items-center gap-1 overflow-hidden'
const ROW_TEXT_FLOOR_NAME = 'min-w-[5ch]'
const ROW_TEXT_FLOOR_STATE = 'min-w-[4ch]'
/** The agent name beside a row chip: yields first (by a shrink weight no other
 *  item comes near). `SessionRow` also hides it on a meta line under 10rem,
 *  where it could only show "k…" and its gap is the room "#" plus a few
 *  characters of the channel need. Lives here so the chip's own fit and the
 *  order it depends on are read together. */
export const CODE_CHANNEL_AGENT_FIT = 'min-w-0 shrink-[1000]'

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

export function CodeChannelBadge({ info, variant }: { info: CodeChannelInfo; variant: 'row' | 'header' }) {
  const name = codeChannelName(info)
  const state = statusLabel(info.status)
  const header = variant === 'header'
  const size = header ? 11 : 10
  const tone = info.status === 'archived'
    // Dashed as well as greyed, so an archived chip still reads differently
    // from an active one when it is down to its icons.
    ? 'border-border border-dashed bg-bg-elevated/60 text-muted'
    : 'border-border bg-accent-subtle text-text'
  const StateIcon = info.status === 'archived' ? Archive : info.status === 'wrapped_up' ? CircleCheck : null
  const stateTone = info.status === 'wrapped_up' ? 'text-ok' : 'text-muted'
  // A state chip's floor holds the Slack mark AND its state glyph, so the
  // glyph that tells archived from active is never the thing cut (the
  // narrowest line's smaller floor sits inline on the chip below).
  const rowFloor = StateIcon ? 'min-w-[30px]' : 'min-w-[20px]'
  const sizing = header
    ? 'min-w-0 max-w-full px-2 py-0.5 text-[11px] rounded-md pointer-events-auto'
    : `${ROW_CHIP_FIT} ${rowFloor} py-0 text-[10px] leading-[14px] font-normal`
  const linkGlyph = info.web_url
    ? <ExternalLink size={header ? 10 : 9} className="shrink-0 text-muted" aria-hidden="true" data-testid="code-channel-link-glyph" />
    : null
  // The header has room: name, then any state, then the link glyph.
  const headerText = (
    <>
      {name && <span className="truncate min-w-0 max-w-[32ch] text-muted" dir="auto" data-testid="code-channel-name">{name}</span>}
      {StateIcon && (
        <span className={`inline-flex min-w-0 items-center gap-0.5 ${stateTone}`} data-testid="code-channel-status">
          <StateIcon size={size - 1} className="shrink-0" aria-hidden="true" />
          <span className="truncate min-w-0 whitespace-nowrap">{state}</span>
        </span>
      )}
      {linkGlyph}
    </>
  )
  // The row's meta line has room for one word: an active channel's name, or an
  // archived or wrapped-up one's state (the name stays in the accessible name
  // and the tooltip). See ROW_TEXT_FIT for how it gives up width.
  const rowWord = state || name || i18nT('components.codeChannelBadge.label_short')
  const rowText = (
    <>
      {name && state && <span className="sr-only" dir="auto" data-testid="code-channel-name">{name}</span>}
      {StateIcon && (
        // Two tones as well as two glyphs: an archived channel is greyed, a
        // finished task reads as done. The glyph stays beside the mark.
        <StateIcon size={size - 1} className={`shrink-0 ${stateTone}`} aria-hidden="true" data-testid="code-channel-status-glyph" />
      )}
      <span className={`${ROW_TEXT_FIT} ${state ? ROW_TEXT_FLOOR_STATE : ROW_TEXT_FLOOR_NAME}`} data-testid="code-channel-text">
        {/* An unnamed chip with a state shows only the state word: the word
            that says what changed is the one worth the width, and the
            accessible name and tooltip already say it is a code channel. */}
        <span
          className={`min-w-0 truncate ${state ? stateTone : ''}`}
          dir="auto"
          aria-hidden={!state && !name ? true : undefined}
          data-testid={state ? 'code-channel-status' : name ? 'code-channel-name' : undefined}
        >{rowWord}</span>
        {linkGlyph}
      </span>
    </>
  )
  const body = (
    <>
      {/* The Slack mark: a fixed box that never shrinks, first in the chip so
          a wrap never takes it. */}
      <span className="inline-flex shrink-0" style={{ width: size, height: size }} aria-hidden="true" data-testid="code-channel-slack-mark">
        <ChannelBrandIcon channel="slack" size={size} />
      </span>
      {header ? (
        <span className="shrink-0 whitespace-nowrap">{i18nT('components.codeChannelBadge.label')}</span>
      ) : (
        <span className="sr-only">{i18nT('components.codeChannelBadge.label')}</span>
      )}
      {header ? headerText : rowText}
    </>
  )
  if (!info.web_url) {
    return (
      <span
        className={`${CHIP_BASE} ${sizing} ${tone} ${header ? '' : '@max-[10rem]/rowmeta:gap-0.5 @max-[10rem]/rowmeta:px-0.5 @max-[8.5rem]/rowmeta:px-px'} ${header ? '' : StateIcon ? '@max-[8.5rem]/rowmeta:min-w-[26px]' : '@max-[8.5rem]/rowmeta:min-w-[14px]'}`}
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
      className={`${CHIP_BASE} ${sizing} ${tone} ${header ? '' : '@max-[10rem]/rowmeta:gap-0.5 @max-[10rem]/rowmeta:px-0.5 @max-[8.5rem]/rowmeta:px-px'} ${header ? '' : StateIcon ? '@max-[8.5rem]/rowmeta:min-w-[26px]' : '@max-[8.5rem]/rowmeta:min-w-[14px]'} hover:text-text hover:border-accent focus-ring`}
      title={codeChannelTitle(info)}
    >
      {body}
      <span className="sr-only">{i18nT('components.codeChannelBadge.opens_in_slack')}</span>
    </a>
  )
}
