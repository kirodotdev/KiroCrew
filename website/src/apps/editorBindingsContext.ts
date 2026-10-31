import { createContext } from 'react'

/**
 * How the code editor reaches the bindings installed apps contributed for the file
 * it shows (`contributes.editorBindings`). The panel that owns the file sets it; the
 * editor asks `bound` before it arms a modifier-click and `fire` when a chord or a
 * click lands. `null` (the default) means no app may bind anything in this editor.
 *
 * Triggers are canonical spellings (`shift+f12`, `click:alt`), the one form the
 * manifest, the matcher and the POST share. The host's own bindings run first: the
 * editor only asks here for an event nothing of its own handled.
 */
export interface EditorBindingDispatcher {
  /** Whether an enabled app bound this trigger for the file. */
  bound: (trigger: string) => boolean
  /** Fire the binding bound to `trigger` at the caret; false when none is. */
  fire: (trigger: string, at: { line: number; character: number }) => boolean
}

export const EditorBindingsContext = createContext<EditorBindingDispatcher | null>(null)
