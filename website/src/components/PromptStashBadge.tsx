import { Archive, ArchiveRestore, Trash2 } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { DropdownMenuItem } from './ui/dropdown-menu'
import type { PromptStashState } from '../hooks/usePromptStash'
import { PROMPT_STASH_MAX } from '../utils/promptStash'

const MENU_ROW = 'w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left group disabled:cursor-default disabled:hover:bg-transparent'
export const DROPDOWN_ROW = 'w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer text-left group data-[disabled]:opacity-100'

/**
 * The pointer and touch paths for the prompt stash: "Stash draft", "Restore
 * stashed draft" and "Discard stashed draft" rows. The rows stay when disabled
 * so the menu does not change shape between keystrokes.
 *
 * Two hosts, one definition (the same arrangement as ChatInput's
 * `collapseMenuRow`): the composer's hand-rolled "+" drop-up on a pointer
 * device takes plain buttons (`host="plus"`), and the touch overflow -- the
 * Radix `DropdownMenu` behind `composer-more-trigger` -- takes
 * `DropdownMenuItem`s (`host="dropdown"`) so the rows type-ahead, arrow-key
 * and close exactly like their Sketch and collapse siblings. The touch host
 * exists because a touch keyboard has no Cmd/Ctrl and `directFilePicker`
 * never mounts the "+" menu, which would otherwise leave stash and restore
 * unreachable on a phone (`narrow-viewport-required`).
 */
export function PromptStashMenuItems({
  stash,
  chordLabel,
  touch = false,
  onPicked,
  host = 'plus',
  divider = true,
}: {
  stash: Pick<PromptStashState, 'canStash' | 'canRestore' | 'count' | 'stashDisabledReason' | 'stash' | 'restoreLatest' | 'canDiscard' | 'discardLatest'>
  chordLabel: string
  /** A touch screen: no shortcut tooltips. */
  touch?: boolean
  /** Close the menu after a pick. The dropdown host closes itself on select,
   *  so it may pass a no-op. */
  onPicked: () => void
  host?: 'plus' | 'dropdown'
  /** Rule above the rows. Off when they are the menu's only content. */
  divider?: boolean
}) {
  const wrap = `${divider ? 'mt-2 pt-2 border-t border-border ' : ''}flex flex-col gap-0.5`
  // One line per row. A second line appears only for a reason the row cannot
  // show by itself (the stash is full, attachments, a draft in the way); what
  // an enabled row does and its shortcut sit in its tooltip on a pointer device.
  const stashNote = stash.stashDisabledReason === 'full'
    ? i18nT('components.promptStash.notice_full', { max: PROMPT_STASH_MAX })
    : stash.stashDisabledReason === 'attachments'
      ? i18nT('components.promptStash.notice_attachments')
      : null
  const restoreNote = stash.canRestore || stash.count === 0
    ? null
    : stash.stashDisabledReason === 'attachments'
      ? i18nT('components.promptStash.notice_attachments')
      : i18nT('components.promptStash.menu_restore_has_draft')
  const stashTitle = touch || !stash.canStash ? undefined : i18nT('components.promptStash.menu_stash_desc', { chord: chordLabel })
  const restoreTitle = touch || !stash.canRestore ? undefined : i18nT('components.promptStash.menu_restore_desc', { count: stash.count, chord: chordLabel })
  const discardTitle = touch || !stash.canDiscard ? undefined : i18nT('components.promptStash.menu_discard_desc')
  const rowBody = (Icon: typeof Archive, label: string, note: string | null) => (
    <>
      <Icon size={14} className="w-4 shrink-0 text-muted lucide-inline group-disabled:opacity-40 group-data-[disabled]:opacity-40" />
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-text group-disabled:opacity-40 group-data-[disabled]:opacity-40">{label}</div>
        {note && <div className="text-[11px] text-muted leading-snug">{note}</div>}
      </div>
    </>
  )
  const stashBody = rowBody(Archive, i18nT('components.promptStash.menu_stash'), stashNote)
  const restoreBody = rowBody(ArchiveRestore, i18nT('components.promptStash.menu_restore'), restoreNote)
  // The way out of a stash the user no longer wants: drops one draft (the
  // restored one while it is unchanged in the input, else the latest), and the
  // status line offers Undo, so a stray pick costs nothing.
  const discardBody = rowBody(Trash2, i18nT('components.promptStash.menu_discard'), null)
  // Restore and Discard only once something is stashed: with an empty stash
  // they can do nothing, and two greyed rows made the menu long for no reason.
  // Discard also covers a restored draft still unchanged in the input.
  const showRestore = stash.count > 0
  const showDiscard = stash.count > 0 || stash.canDiscard
  if (host === 'dropdown') {
    return (
      <div className={wrap} data-testid="prompt-stash-menu">
        <DropdownMenuItem asChild disabled={!stash.canStash} onSelect={() => { onPicked(); stash.stash() }}>
          {/* The row's look sits on the child, not on the primitive. */}
          <button type="button" data-testid="prompt-stash-menu-stash" title={stashTitle} className={DROPDOWN_ROW}>
            {stashBody}
          </button>
        </DropdownMenuItem>
        {showRestore && (
          <DropdownMenuItem asChild disabled={!stash.canRestore} onSelect={() => { onPicked(); stash.restoreLatest() }}>
            {/* The row's look sits on the child, not on the primitive. */}
            <button type="button" data-testid="prompt-stash-menu-restore" title={restoreTitle} className={DROPDOWN_ROW}>
              {restoreBody}
            </button>
          </DropdownMenuItem>
        )}
        {showDiscard && (
          <DropdownMenuItem asChild disabled={!stash.canDiscard} onSelect={() => { onPicked(); stash.discardLatest() }}>
            {/* The row's look sits on the child, not on the primitive. */}
            <button type="button" data-testid="prompt-stash-menu-discard" title={discardTitle} className={DROPDOWN_ROW}>
              {discardBody}
            </button>
          </DropdownMenuItem>
        )}
      </div>
    )
  }
  return (
    <div className={wrap} data-testid="prompt-stash-menu">
      <button
        type="button"
        data-testid="prompt-stash-menu-stash" title={stashTitle}
        disabled={!stash.canStash}
        onClick={() => { onPicked(); stash.stash() }}
        className={MENU_ROW}
      >
        {stashBody}
      </button>
      {showRestore && (
        <button
          type="button"
          data-testid="prompt-stash-menu-restore" title={restoreTitle}
          disabled={!stash.canRestore}
          onClick={() => { onPicked(); stash.restoreLatest() }}
          className={MENU_ROW}
        >
          {restoreBody}
        </button>
      )}
      {showDiscard && (
        <button
          type="button"
          data-testid="prompt-stash-menu-discard" title={discardTitle}
          disabled={!stash.canDiscard}
          onClick={() => { onPicked(); stash.discardLatest() }}
          className={MENU_ROW}
        >
          {discardBody}
        </button>
      )}
    </div>
  )
}
