/**
 * The ONE `/api/file-read` fetch for a side-panel tab, and the cache key it
 * lives under.
 *
 * Three callers read a file into a tab: `usePanelDocumentActions.openFile`
 * (chip / tree click), `ChatPage`'s cold-tab hydration (after a reload), and
 * `MarkdownPanel`'s own disk refresh (Refresh, Cancel, the file watch). The
 * first two share the `['file-read', path]` React Query entry, so they must
 * store one shape; and all three must carry the backend's binary verdict with
 * the text, because the verdict decides whether the panel offers an editor at
 * all. It is read from the `X-File-Binary` header, not from the body: a `.json`
 * TEXT file is served as `application/json` too, so the content type cannot
 * tell the two apart.
 */
import { fileReadUrl } from './fileReadUrl'

/** What a `['file-read', path]` cache entry holds. */
export interface FileReadResult {
  /** The file's text. `''` when the read failed and when the file is binary --
   *  a binary response's body is an envelope, never the file. */
  text: string
  ok: boolean
  status: number
  /** The backend's verdict for the bytes THIS read saw. */
  binary: boolean
}

/** The shared cache key. Never spell `['file-read', path]` by hand. */
export function fileReadQueryKey(filePath: string): readonly [string, string] {
  return ['file-read', filePath]
}

/** Read a file for a panel tab. Never throws for an HTTP status -- `ok` carries
 *  it, so each caller decides what a 404 means for its own surface. A transport
 *  failure still rejects. */
export async function fetchFileRead(filePath: string, signal?: AbortSignal): Promise<FileReadResult> {
  const res = await fetch(fileReadUrl(filePath), signal ? { signal } : undefined)
  const binary = res.headers.get('X-File-Binary') === 'true'
  return {
    text: res.ok && !binary ? await res.text() : '',
    ok: res.ok,
    status: res.status,
    binary,
  }
}
