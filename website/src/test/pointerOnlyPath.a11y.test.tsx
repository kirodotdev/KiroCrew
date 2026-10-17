/**
 * One invariant, across every chat surface that discloses a file or folder path:
 *
 *   A path carried in a native `title` must ALSO be reachable without a pointer.
 *
 * `title` opens on pointer hover only, and no browser opens it on keyboard
 * focus, so a path left in `title` alone reaches nobody using a keyboard or a
 * screen reader. Two mechanisms carry it instead, and which one a site gets
 * depends on whether anything ever focuses it:
 *
 * - An element nothing focuses -- an inert transcript chip, a menu item -- carries
 *   the path as visually-hidden TEXT, with the visible short label aria-hidden so
 *   the path is spoken once and whole. Text is read wherever the element is read,
 *   including browse mode, with no dependence on how a screen reader treats a
 *   named non-interactive role. Every hidden span is `select-none`, because
 *   `sr-only` keeps text in the DOM and a copied message must carry the label a
 *   reader sees rather than the hidden path.
 * - A container whose children ARE focusable -- a composer preview tile with its
 *   own buttons -- is a `role="group"` NAMED by the path. A group's name is
 *   announced when focus enters it, which is the reliable case, and it is also
 *   what tells the tiles' identical `Remove` buttons apart.
 *
 * Each test asserts BOTH halves: the pointer route still works and the
 * non-pointer route exists. Asserting only the second would pass on a change
 * that dropped the tooltip.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'

// Only the prose of a bubble goes through the renderer; the path chips are its
// siblings, so echoing the markdown keeps them in the tree without pulling the
// markdown pipeline into an accessibility test.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))

import { renderUserContent } from '../pages/chat/ChatPageMessageContent'
import ChatInput from '../components/ChatInput'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import { FOLDER_PATH_SEP } from '../utils/folderTree'

beforeEach(() => {
  localStorage.clear()
})

/** The visually-hidden half of a chip: text present for a reader, not on screen. */
function srText(el: HTMLElement): string {
  return [...el.querySelectorAll('.sr-only')].map(n => n.textContent || '').join(' ').trim()
}
/** What a reader would NOT hear, because it is aria-hidden. */
function mutedText(el: HTMLElement): string {
  return [...el.querySelectorAll('[aria-hidden="true"]')].map(n => n.textContent || '').join(' ').trim()
}

// ── Sent-message chips ──────────────────────────────────────────────────────
//
// A chip degrades to an inert span on a host with no file viewer (a split pane,
// a Crew Member DM). That inert branch is what these pin: its clickable sibling
// is a real button and is named with the path already.

describe('inert sent-message chips read out the full path, not only the tooltip', () => {
  /** The inert host: no handlers at all, so every chip takes its degraded form. */
  const inert = (content: string, meta?: Record<string, unknown>) =>
    render(<div>{renderUserContent({ content, meta })}</div>)

  const chipFor = (c: HTMLElement, path: string) =>
    c.querySelector(`[title="${path}"]`) as HTMLElement

  it('reads out the full path of an inert folder chip, once', () => {
    const dir = '/repo/main/website/src'
    const { container } = inert('look in [attached_dir 1] /repo/main/website/src for dead code', { dirs: [dir] })
    // No button: this host cannot open the folder, so nothing focuses the chip.
    expect(screen.queryByRole('button')).toBeNull()
    const chip = chipFor(container, dir)
    // The pointer route is intact.
    expect(chip).toHaveAttribute('title', dir)
    // ON SCREEN it is only the short label; the directories are read-only text.
    expect(chip).toHaveTextContent('@src')
    expect(srText(chip)).toBe(dir)
    // The visible label is muted, so the basename is not spoken twice.
    expect(mutedText(chip)).toContain('@src')
  })

  it('reads out the full path of an inert file-mention chip, once', () => {
    const file = '/home/u/notes/todo.md'
    const { container } = inert('please read [attached_file 1] /home/u/notes/todo.md and summarise', { files: [file] })
    const chip = chipFor(container, file)
    expect(chip).toHaveAttribute('title', file)
    expect(srText(chip)).toBe(file)
    expect(mutedText(chip)).toBe('@todo.md')
  })

  it('reads out the inert attachment card once, not its filename twice', () => {
    // This card's tooltip is a localized sentence CONTAINING the path, which
    // also says the card cannot be opened here. Reading the visible label too
    // would say the filename a second time.
    const pdf = '/home/u/q3/report.pdf'
    const sentence = i18nT('pages.chatPage.attached_file_inert', { path: pdf })
    const { container } = inert('[attached_file 1] /home/u/q3/report.pdf', { files: [pdf] })
    const card = chipFor(container, sentence)
    expect(card).toHaveAttribute('title', sentence)
    expect(srText(card)).toBe(sentence)
    expect(mutedText(card)).toContain('report.pdf')
    expect(sentence).toContain(pdf)
  })

  it('keeps the hidden path out of a copied selection', () => {
    // `sr-only` hides text visually but leaves it in the DOM, so selecting a
    // bubble and copying it would paste the path alongside the label a reader
    // sees. Every hidden span is `select-none` for that reason -- the same
    // aria-hidden + select-none pairing RepoSwitcher uses for its sentinels.
    const dir = '/repo/main/website/src'
    const pdf = '/home/u/q3/report.pdf'
    const { container } = inert(
      'look in [attached_dir 1] /repo/main/website/src and [attached_file 1] /home/u/q3/report.pdf',
      { dirs: [dir], files: [pdf] },
    )
    const hidden = [...container.querySelectorAll('.sr-only')]
    expect(hidden.length).toBeGreaterThan(0)
    for (const el of hidden) expect(el.className).toContain('select-none')
  })

  it('keeps the inert chip inert: a naming role is not added in place of an action', () => {
    // The degrade contract is that an inert chip must not LOOK actionable. The
    // path is text, so it needs no role at all.
    const dir = '/repo/main/website/src'
    const { container } = inert('look in [attached_dir 1] /repo/main/website/src for dead code', { dirs: [dir] })
    expect(chipFor(container, dir).getAttribute('role')).toBeNull()
    expect(chipFor(container, dir).getAttribute('tabindex')).toBeNull()
  })

  it('tells two same-named files in different directories apart', () => {
    const a = '/home/u/q3/report.pdf'
    const b = '/home/u/q4/report.pdf'
    const { container } = inert('[attached_file 1] /home/u/q3/report.pdf\n[attached_file 2] /home/u/q4/report.pdf', {
      files: [a, b],
    })
    for (const path of [a, b]) {
      const sentence = i18nT('pages.chatPage.attached_file_inert', { path })
      expect(srText(chipFor(container, sentence))).toContain(path)
    }
  })
})

// ── Folder-move menu ────────────────────────────────────────────────────────

describe('folder-move menu items read out their nesting path', () => {
  const Item = ({ title, children }: { title?: string; children?: React.ReactNode }) => (
    <div role="menuitem" title={title}>{children}</div>
  )
  const folders = [
    { id: 'a', name: 'Work', parent_id: null },
    { id: 'b', name: 'Notes', parent_id: 'a' },
    { id: 'c', name: 'Notes', parent_id: null },
  ] as never

  it('names a nested folder by its path alone, and a top-level one by its name', () => {
    // Two folders called "Notes"; only the indent separates them on screen. The
    // nested one is named by its path WITHOUT the leaf name repeated first.
    render(<FolderPickerItems folders={folders} onPick={vi.fn()} Item={Item} />)
    const nested = screen.getByRole('menuitem', { name: `Work${FOLDER_PATH_SEP}Notes` })
    // Assert the MECHANISM, not only the computed name: `title` is also on the
    // item, so a name that came from the tooltip alone would pass the query above
    // while leaving nothing for a reader that ignores `title`.
    expect(srText(nested)).toBe(`Work${FOLDER_PATH_SEP}Notes`)
    expect(mutedText(nested)).toBe('Notes')

    // The top-level "Notes" path IS its name, so nothing hidden is added and the
    // visible name stays the one a reader gets.
    const top = screen.getByRole('menuitem', { name: 'Notes' })
    expect(srText(top)).toBe('')
    expect(mutedText(top)).toBe('')

    // Hidden text stays out of a copied selection, same as the chips.
    for (const el of document.querySelectorAll('.sr-only')) {
      expect(el.className).toContain('select-none')
    }
  })
})

// ── Composer preview strip ──────────────────────────────────────────────────

describe('composer preview tiles name the full path, not only the tooltip', () => {
  const props = { value: '', onChange: vi.fn(), onSend: vi.fn() }

  it('names an image tile with the full path', () => {
    const png = '/home/u/shots/screenshot.png'
    renderWithProviders(<ChatInput {...props} pendingFiles={[png]} />)
    const tile = screen.getByRole('group', { name: png })
    expect(tile).toHaveAttribute('title', png)
  })

  it('names a non-image tile with the full path its visible text omits', () => {
    const txt = '/home/u/notes/todo.txt'
    renderWithProviders(<ChatInput {...props} pendingFiles={[txt]} />)
    const tile = screen.getByRole('group', { name: txt })
    expect(tile).toHaveAttribute('title', txt)
    expect(tile).toHaveTextContent('todo.txt')
    expect(tile.textContent).not.toContain('/home/u/notes')
  })

  it('names a folder tile with the full path', () => {
    const dir = '/repo/main/website/docs'
    renderWithProviders(<ChatInput {...props} pendingDirs={[dir]} />)
    const tile = screen.getByRole('group', { name: dir })
    expect(tile).toHaveAttribute('title', dir)
  })

  it('puts each bare-verb remove button inside its own file group', () => {
    // Two files staged means two buttons both named just "Remove". The group
    // each sits in is what says WHICH file it removes, and a group's name IS
    // announced when focus moves into it.
    const a = '/home/u/a/report.txt'
    const b = '/home/u/b/report.txt'
    const onRemoveFile = vi.fn()
    renderWithProviders(<ChatInput {...props} pendingFiles={[a, b]} onRemoveFile={onRemoveFile} />)
    const remove = i18nT('components.chatInput.remove')
    for (const path of [a, b]) {
      const tile = screen.getByRole('group', { name: path })
      fireEvent.click(within(tile).getByRole('button', { name: remove }))
      expect(onRemoveFile).toHaveBeenLastCalledWith(path)
    }
  })

  it('puts the remove-folder button inside its folder group', () => {
    const dir = '/repo/main/website/docs'
    const onRemoveDir = vi.fn()
    renderWithProviders(<ChatInput {...props} pendingDirs={[dir]} onRemoveDir={onRemoveDir} />)
    const tile = screen.getByRole('group', { name: dir })
    const label = i18nT('components.filePickerMenu.remove_folder')
    fireEvent.click(within(tile).getByRole('button', { name: label }))
    expect(onRemoveDir).toHaveBeenCalledWith(dir)
  })
})
