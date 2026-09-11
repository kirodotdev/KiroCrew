/**
 * Browser-panel element annotations: the renderer-side pure pieces.
 *
 * The overlay that highlights, picks and edits lives in the page (see
 * electron/browser-annotate.js); the panel only mirrors its state. This module
 * holds what the panel needs that is DOM-free and testable: the short element
 * description shown in the list, the file the chat receives, and the window
 * event that hands both to ChatPage. The agent-facing draft text lives in
 * `browserAnnotations.prompt.ts`.
 */

/** Window event: the panel hands a finished annotation set to ChatPage --
 *  `{ slot, files, draft }`. ChatPage uploads the files into the composer's
 *  attachments and puts `draft` into the composer text (NOT sent), so the
 *  user can add a sentence before sending. */
export const PREVIEW_ANNOTATE_EVENT = 'kirocrew-web-preview-annotate'

export interface PreviewAnnotateDetail {
  slot: string
  files: File[]
  draft: string
}

/** Mirrors BrowserAnnotation (electron-bridge.d.ts) structurally so tests and
 *  the prompt module need no ambient types. */
export interface AnnotationItem {
  id: number
  n: number
  note: string
  ref: string
  tag: string
  role: string
  name: string
  text: string
  selector: string
  detached: boolean
}

/** Cap for the element label shown in the list and the draft. */
const LABEL_MAX = 40

function truncate(s: string, max: number = LABEL_MAX): string {
  const t = s.replace(/\s+/g, ' ').trim()
  return t.length > max ? `${t.slice(0, max - 1)}…` : t
}

/** The element's human label: accessible name first, visible text second. */
export function annotationLabel(a: Pick<AnnotationItem, 'name' | 'text'>): string {
  return truncate(a.name || a.text || '')
}

/** Short identification for the list: `button "Save"`, `combobox "Search"`,
 *  `p "Some paragraph…"`. The ROLE when the element has one, else the tag --
 *  the same vocabulary the draft uses, so the row and the line the agent reads
 *  name the element identically; the label tells two same-kind rows apart. */
export function describeAnnotationTarget(a: Pick<AnnotationItem, 'tag' | 'role' | 'name' | 'text'>, roleNames?: Readonly<Record<string, string>>): string {
  const kind = (a.role && roleNames?.[a.role]) || a.role || a.tag
  const label = annotationLabel(a)
  return label ? `${kind} "${label}"` : kind
}

/** ARIA roles whose names mean nothing to a non-developer; each has a plain
 *  `annotate_role_<role>` string. The raw role stays in the draft the agent
 *  reads -- only what the user sees is translated. */
export const OPAQUE_ROLE_KEYS = {
  combobox: 'components.webPreviewPanel.annotate_role_combobox',
  textbox: 'components.webPreviewPanel.annotate_role_textbox',
  searchbox: 'components.webPreviewPanel.annotate_role_searchbox',
  listbox: 'components.webPreviewPanel.annotate_role_listbox',
  menuitem: 'components.webPreviewPanel.annotate_role_menuitem',
  spinbutton: 'components.webPreviewPanel.annotate_role_spinbutton',
} as const

/** Local-time stamp for the screenshot file name (mirrors the sketch pad's
 *  `sketch-<ts>` naming so annotation files sort next to it). */
export function annotationStamp(now: Date = new Date()): string {
  return now.toISOString().replace(/[:.]/g, '-').slice(0, 19)
}

/** Decode a base64 PNG into a File for the upload pipeline. */
export function annotationScreenshotFile(pngBase64: string, stamp: string = annotationStamp()): File {
  const bin = atob(pngBase64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new File([bytes], `browser-annotations-${stamp}.png`, { type: 'image/png' })
}
