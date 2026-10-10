import { Archive, ArchiveRestore } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { DropdownMenuItem } from './ui/dropdown-menu'
import type { PromptStashState } from '../hooks/usePromptStash'
import { PROMPT_STASH_MAX } from '../utils/promptStash'

const MENU_ROW = 'w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left group disabled:cursor-default disabled:hover:bg-transparent'
export const DROPDOWN_ROW = 'w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer text-left group data-[disabled]:opacity-100'

/**
 * The pointer and touch paths for the prompt stash: "Stash draft" and "Restore
 * stashed draft" rows. Both rows stay when disabled so the menu does not change
 * shape between keystrokes.
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
  stash: Pick<PromptStashState, 'canStash' | 'canRestore' | 'count' | 'stashDisabledReason' | 'stash' | 'restoreLatest'>
  chordLabel: string
  /** Use descriptions that name this menu rather than a hardware chord. */
  touch?: boolean
  /** Close the menu after a pick. The dropdown host closes itself on select,
   *  so it may pass a no-op. */
  onPicked: () => void
  host?: 'plus' | 'dropdown'
  /** Rule above the rows. Off when they are the menu's only content. */
  divider?: boolean
}) {
  const wrap = `${divider ? 'mt-2 pt-2 border-t border-border ' : ''}flex flex-col gap-0.5`
  const stashDescription = stash.canStash
    ? i18nT(touch ? 'components.promptStash.menu_stash_desc_touch' : 'components.promptStash.menu_stash_desc', { chord: chordLabel })
    : stash.stashDisabledReason === 'full'
      ? i18nT('components.promptStash.notice_full', { max: PROMPT_STASH_MAX })
      : stash.stashDisabledReason === 'attachments'
        ? i18nT('components.promptStash.notice_attachments')
        : stash.stashDisabledReason === 'empty'
          ? i18nT('components.promptStash.menu_stash_empty')
          : i18nT(touch ? 'components.promptStash.menu_stash_desc_touch' : 'components.promptStash.menu_stash_desc', { chord: chordLabel })
  const stashBody = (
    <>
      <Archive size={14} className="w-4 shrink-0 text-muted lucide-inline group-disabled:opacity-40 group-data-[disabled]:opacity-40" />
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-text group-disabled:opacity-40 group-data-[disabled]:opacity-40">{i18nT('components.promptStash.menu_stash')}</div>
        <div className="text-[11px] text-muted leading-snug">{stashDescription}</div>
      </div>
    </>
  )
  const restoreBody = (
    <>
      <ArchiveRestore size={14} className="w-4 shrink-0 text-muted lucide-inline group-disabled:opacity-40 group-data-[disabled]:opacity-40" />
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-text group-disabled:opacity-40 group-data-[disabled]:opacity-40">{i18nT('components.promptStash.menu_restore')}</div>
        <div className="text-[11px] text-muted leading-snug">
          {!stash.canRestore && stash.count > 0 && stash.stashDisabledReason === 'attachments'
            ? i18nT('components.promptStash.notice_attachments')
            : !stash.canRestore && stash.count > 0
              ? i18nT('components.promptStash.menu_restore_has_draft')
              : stash.count > 0
                ? i18nT(touch ? 'components.promptStash.menu_restore_desc_touch' : 'components.promptStash.menu_restore_desc', { count: stash.count, chord: chordLabel })
                : i18nT('components.promptStash.menu_restore_none')}
        </div>
      </div>
    </>
  )
  if (host === 'dropdown') {
    return (
      <div className={wrap} data-testid="prompt-stash-menu">
        <DropdownMenuItem asChild disabled={!stash.canStash} onSelect={() => { onPicked(); stash.stash() }}>
          {/* The row's look sits on the child, not on the primitive. */}
          <button type="button" data-testid="prompt-stash-menu-stash" className={DROPDOWN_ROW}>
            {stashBody}
          </button>
        </DropdownMenuItem>
        <DropdownMenuItem asChild disabled={!stash.canRestore} onSelect={() => { onPicked(); stash.restoreLatest() }}>
          {/* The row's look sits on the child, not on the primitive. */}
          <button type="button" data-testid="prompt-stash-menu-restore" className={DROPDOWN_ROW}>
            {restoreBody}
          </button>
        </DropdownMenuItem>
      </div>
    )
  }
  return (
    <div className={wrap} data-testid="prompt-stash-menu">
      <button
        type="button"
        data-testid="prompt-stash-menu-stash"
        disabled={!stash.canStash}
        onClick={() => { onPicked(); stash.stash() }}
       
        className={MENU_ROW}
      >
        {stashBody}
      </button>
      <button
        type="button"
        data-testid="prompt-stash-menu-restore"
        disabled={!stash.canRestore}
        onClick={() => { onPicked(); stash.restoreLatest() }}
       
        className={MENU_ROW}
      >
        {restoreBody}
      </button>
    </div>
  )
}
