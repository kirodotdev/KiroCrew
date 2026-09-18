/**
 * Typed client for `POST /api/file-grep` — the Files rail's CONTENT search.
 *
 * Its own module rather than another method on `api/client.ts`: the rail is the
 * only caller, the response carries a shape (a hit, and a hit's location inside
 * a document) that other call sites have no use for, and `client.ts` is ~3.5k
 * lines whose module graph pulls in the query client, the artifact-write
 * bookkeeping and the error journal. Same reasoning that split `apiError.ts` out.
 *
 * The REQUEST still goes through the blessed transport rather than raw `fetch`.
 * That is what carries `X-Session-Key` and, decisively, what runs the
 * session-expiry pipeline: a 403 with `X-Auth-Required` after the dashboard
 * session lapses has to raise the auth banner, not read as "content search
 * failed". `j` also produces the {@link ApiError} a caller branches on by
 * `status` (403 refused root, 404 not a directory, 503 probe capacity) instead
 * of matching message text.
 *
 * Filename search is `api.fileSearch` in `client.ts` and stays there — it feeds
 * the @-mention picker as well as the rail.
 */
import { apiTransport } from './apiTransport'

/** One match. `label` is present only for a hit inside a document. */
export interface FileGrepHit {
  /** Absolute path of the file that matched. */
  file: string
  /** 1-based line, or 0 for a document hit — those have no line to open at. */
  line: number
  /** The matching line, or the matching row/paragraph of a document. */
  preview: string
  /**
   * Where inside a document the match sits, as the backend spells it:
   * `slide 7` or `Sheet1 row 12`. Empty for a `.docx`, which has no navigable
   * position inside it to name. Absent for a plain-text hit, which shows `:line`.
   */
  label?: string
}

export interface FileGrepResponse {
  results: FileGrepHit[]
  /** A cap or the wall-clock budget stopped the search before it finished. */
  truncated: boolean
  /**
   * Which engine answered, surfaced so a slow host is diagnosable. Empty for a
   * request that ran no search (too short a query, no root): naming an engine
   * there would be a guess, and finding out costs the gateway a `$PATH` walk on
   * its event loop.
   */
  engine: 'rg' | 'python' | ''
  /**
   * Documents the budget did not let it open. A FLOOR, not a total — a spent
   * deadline ends the walk, and `truncated` is what says so.
   */
  skipped_docs: number
  /** The canonicalized root actually searched; `''` when nothing was. */
  root: string
}

export async function fileGrep(root: string, q: string): Promise<FileGrepResponse> {
  // POST, with the query in the body: a user searching for a token VALUE types
  // the token, and a request URL is what proxies and access logs retain.
  const response = await apiTransport.post('/api/file-grep', { root, q })
  return (await apiTransport.j(response)) as FileGrepResponse
}
