/**
 * "Add Artifact" — import a file from the user's machine into the library.
 *
 * Semantics: adding a file **copies its text into artifact storage**. From
 * that moment the artifact owns its content, so deleting, moving, or editing
 * the original file never changes the artifact. This is deliberately NOT a
 * file-backed artifact (one carrying `source_path`): a live pointer degrades
 * to a stale snapshot once the original moves, and because content writes are
 * mirrored back to `source_path`, editing the artifact would silently rewrite
 * the user's own file. A copy has neither failure mode.
 *
 * The picker is a plain file input, so the bytes are read in the browser and
 * posted as ordinary artifact content — no new server file-read path, and it
 * works the same on Linux, macOS, and Windows. The dashboard is a loopback
 * origin, so "local operation" holds: the bytes never leave the machine.
 */
import type { Artifact } from '../types'

/**
 * Extension → artifact kind for importable files.
 *
 * Mirrors `_EXT_KIND_MAP` in `src/kiro_crew/artifacts.py` (the backend's
 * kind-inference map for file-backed artifacts). Both answer the same
 * question — which file extension means which artifact kind — so they are
 * held identical by `test/test_artifact_import_parity.py`, which parses this
 * object and fails if the two drift.
 *
 * Every kind here has a real renderer: `markdown` / `json` / `svg` / `text`
 * render natively through `ArtifactBodyNative`, and `html` renders in the
 * sandboxed iframe. The other kinds are deliberately absent — `widget` is an
 * agent-authored mcwidget body rather than a file on disk, `webapp` is a
 * deploy control card, and `image` has no dashboard renderer at all.
 */
export const IMPORTABLE_EXT_KINDS: Record<string, Artifact['kind']> = {
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.html': 'html',
  '.htm': 'html',
  '.svg': 'svg',
  '.json': 'json',
  '.txt': 'text',
}

/**
 * Content cap, mirroring `MAX_CONTENT_BYTES` in `src/kiro_crew/artifacts.py`.
 * Checked client-side so an oversize pick fails immediately with a clear
 * message instead of after uploading 25 MiB only to be refused.
 */
export const MAX_IMPORT_BYTES = 26_214_400

/** Value for the file input's `accept` attribute. */
export const IMPORT_ACCEPT = Object.keys(IMPORTABLE_EXT_KINDS).join(',')

/** Human-readable extension list, for the "unsupported type" message. */
export const IMPORTABLE_EXT_LIST = Object.keys(IMPORTABLE_EXT_KINDS).join(' ')

/** Why a chosen file cannot become an artifact. */
export type ImportRejection =
  | 'unsupported-type'
  | 'too-large'
  | 'empty'
  | 'not-text'
  | 'unreadable'

/** What to send to `POST /api/artifacts` for an accepted file. */
export interface ImportPlan {
  name: string
  kind: Artifact['kind']
  content: string
}

export type ImportPlanResult =
  | { ok: true; plan: ImportPlan }
  | { ok: false; reason: ImportRejection }

/**
 * Lowercased final extension of a filename, or `''` when it has none.
 *
 * A leading-dot name with no other dot (`.gitignore`, or a bare `.md`) is a
 * dotfile, not an extension, so it reports `''` and is refused rather than
 * being guessed at.
 */
export function extensionOf(filename: string): string {
  const base = filename.slice(filename.lastIndexOf('/') + 1)
  const dot = base.lastIndexOf('.')
  return dot <= 0 ? '' : base.slice(dot).toLowerCase()
}

/** The artifact kind for a filename, or `null` when the type is not importable. */
export function kindForFilename(filename: string): Artifact['kind'] | null {
  return IMPORTABLE_EXT_KINDS[extensionOf(filename)] ?? null
}

/**
 * Decode file bytes as strict UTF-8 text, or `null` when they are not text.
 *
 * `null` covers both invalid UTF-8 and text containing a NUL — i.e. a binary
 * file whatever its extension claims. Without this, a renamed `.txt` would be
 * stored as a wall of U+FFFD replacement characters that no renderer can
 * display. "Only renderable types" has to be enforced on the bytes, not just
 * the extension: the extension is a claim, the bytes are the evidence.
 */
export function decodeTextStrict(bytes: ArrayBuffer): string | null {
  let text: string
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
  return text.includes('\u0000') ? null : text
}

/**
 * Validate a picked file and read it into an `ImportPlan`, or explain why not.
 *
 * Order matters: the cheap metadata checks (extension, size) run before the
 * file is read, so an unsupported or oversize pick never buffers its bytes.
 */
export async function planFileImport(file: File): Promise<ImportPlanResult> {
  const kind = kindForFilename(file.name)
  if (!kind) return { ok: false, reason: 'unsupported-type' }
  if (file.size > MAX_IMPORT_BYTES) return { ok: false, reason: 'too-large' }
  if (file.size === 0) return { ok: false, reason: 'empty' }
  let bytes: ArrayBuffer
  try {
    bytes = await file.arrayBuffer()
  } catch {
    // A picked file can become unreadable before it is read: an ejected
    // volume, a dropped network share, or the file being deleted or replaced
    // in the interim. Report it as a rejection — letting the promise reject
    // would surface as an unhandled rejection and abort the import with no
    // feedback to the user at all.
    return { ok: false, reason: 'unreadable' }
  }
  const content = decodeTextStrict(bytes)
  if (content === null) return { ok: false, reason: 'not-text' }
  return { ok: true, plan: { name: file.name, kind, content } }
}

/**
 * True when the store did NOT round-trip the imported text verbatim.
 *
 * The artifacts API redacts credential-like material out of `content` on every
 * READ (`handlers/artifacts.py::_serialize` runs `redact_exfiltration_urls()` +
 * `redact_credentials()`), while the POST stores what it was given. A file
 * carrying real credential material therefore lands verbatim but reads back as
 * placeholders — and the next edit from the dashboard would save those
 * placeholders over the imported text, silently destroying it.
 *
 * Comparing the created artifact's returned content against what was sent
 * detects exactly that case. `returned` is typed `unknown` because a response
 * without a `content` field must not be read as "redacted": absent content is
 * not evidence of a mismatch.
 */
export function wasContentRedacted(sent: string, returned: unknown): boolean {
  return typeof returned === 'string' && returned !== sent
}
