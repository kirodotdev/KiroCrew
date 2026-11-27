/**
 * An artifact's tags as removable chips, then an add-a-tag field behind a "+ tag" button.
 * Adding and removing post metadata only (no version bump). Shared by the generic
 * artifact header and the canvas page's Tags panel, which read the same page state.
 */
import type { MutableRefObject } from 'react'
import { Plus, X } from 'lucide-react'
import HoverTip from './HoverTip'
import { i18nT } from '../i18n/t'
import type { useImeGuard } from '../hooks/useImeGuard'

const TOOLBAR_CHIP_CLASS = 'inline-flex items-center gap-1.5 h-8 px-2.5 text-[12px] rounded-md box-border'

export function ArtifactTagChips({ tags, addTag, removeTag, stateRead }: {
  tags: string[]
  addTag: (raw: string) => void
  removeTag: (tag: string) => void
  /** The page's tag-entry state, so the field survives a re-render of its host. */
  stateRead: {
    addingTag: boolean
    setAddingTag: (open: boolean) => void
    newTag: string
    setNewTag: (text: string) => void
    ime: ReturnType<typeof useImeGuard>
    refocusAddTagRef: MutableRefObject<boolean>
    addTagButtonRef: MutableRefObject<HTMLButtonElement | null>
  }
}) {
  const { addingTag, setAddingTag, newTag, setNewTag, ime, refocusAddTagRef, addTagButtonRef } = stateRead
  // A key that closes the tag input unmounts the focused field, so the + Tag
  // button takes focus back as it mounts. Enter and Space would also activate
  // that newly focused button, so their default is cancelled; Escape and comma
  // activate nothing and keep theirs.
  const closeTagInputByKey = (e: { key: string; preventDefault: () => void }) => {
    if (e.key === 'Enter' || e.key === ' ') e.preventDefault()
    refocusAddTagRef.current = true
  }

  return (
    <>
      {tags.map((t) => (
        <span key={t} className={`${TOOLBAR_CHIP_CLASS} bg-bg-elevated border border-border text-muted group`}>
          {/* Own element: the remove button's HoverTip keeps an sr-only copy of
              its label in this chip, so the bare text node would no longer be
              the chip's whole text. */}
          <span>{t}</span>
          <HoverTip label={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}>
            <button
              type="button"
              // The chip, and this focused button with it, leaves the page
              // once the write lands, so focus moves to + Tag first.
              onClick={() => { removeTag(t); addTagButtonRef.current?.focus() }}
              className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-within:opacity-100 hover:text-danger transition-opacity bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
              aria-label={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}
            >
              <X size={12} />
            </button>
          </HoverTip>
        </span>
      ))}
      {addingTag ? (
        <input
          type="text"
          value={newTag}
          onChange={e => setNewTag(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              // Rule 2 shape: this handler carries more than Enter (comma/space
              // also commit), so only the Enter path is gated. Rule 1: single-line
              // input, so the guard alone is enough.
              if (ime.isComposing(e)) return
              // addTag closes the input for any non-blank text, a duplicate included.
              if (newTag.trim()) closeTagInputByKey(e)
              addTag(newTag)
            }
            if (e.key === ',' || e.key === ' ') {
              // Space cycles IME candidates mid-composition; committing here
              // would post the composing buffer and kill candidate selection.
              // The claim is key-agnostic: it consumes the separator only
              // when the IME is not itself using the keypress.
              if (ime.claimEnter(e) && newTag.trim()) { closeTagInputByKey(e); addTag(newTag) }
            }
            if (e.key === 'Escape') { ime.reset(); setNewTag(''); closeTagInputByKey(e); setAddingTag(false) }
          }}
          {...ime.bindComposition({
            onBlur: () => {
              if (newTag.trim()) addTag(newTag)
              else setAddingTag(false)
            },
          })}
          autoFocus
          placeholder={i18nT('pages.artifactDetailPage.tag')}
          className={`${TOOLBAR_CHIP_CLASS} bg-bg-elevated border border-accent text-text outline-hidden focus-ring`}
          style={{ width: '90px' }}
          aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
        />
      ) : (
        <HoverTip label={i18nT('pages.artifactDetailPage.add_a_tag_comma_separated_tags_supported')}>
          <button
            ref={el => {
              addTagButtonRef.current = el
              if (!el || !refocusAddTagRef.current) return
              refocusAddTagRef.current = false
              el.focus()
            }}
            type="button"
            onClick={() => setAddingTag(true)}
            className={`${TOOLBAR_CHIP_CLASS} border border-dashed border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors`}
            aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
          >
            <Plus size={13} /> {i18nT('pages.artifactDetailPage.tag_2')}
          </button>
        </HoverTip>
      )}
    </>
  )
}
