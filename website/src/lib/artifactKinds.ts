import type { Artifact } from '../types'

/**
 * The document types a person may choose from the artifact page's type control.
 *
 * Mirrors `USER_SELECTABLE_KINDS` in `src/kiro_crew/artifacts.py`, which the API
 * enforces — this list only decides what the UI offers. Keep the two in step.
 *
 * Deliberately the same set as `isEditableKind`: `widget` and `html` render in a
 * sandboxed iframe with no editor, so offering them would let someone strand the
 * document they are typing in, and `webapp` carries deploy metadata that a
 * hand-flip would desynchronise.
 *
 * The control exists because some types cannot be detected from content. Markdown
 * and plain text accept the identical bytes and differ only in whether `#` means
 * "heading" or a literal hash — that is intent, and intent has to be stated.
 */
export const USER_SELECTABLE_KINDS: Array<Artifact['kind']> = ['markdown', 'text', 'json', 'svg']
