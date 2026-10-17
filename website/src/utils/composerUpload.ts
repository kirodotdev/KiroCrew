/**
 * The composer upload body (`POST /api/upload/file`), shared by every surface
 * that stages composer attachments: the dashboard client and the ChatEmbed
 * composer. Only the transport differs -- the dashboard calls `fetch`, an app
 * calls its scoped client -- so the caller passes the POST and this owns the
 * rest: client-side image downscaling, the multipart body, the `paths`
 * contract and pairing each resize with the server path it landed at.
 */
import { i18nT } from '../i18n/t'
import { resizeImageForModel, type ResizeInfo } from './resizeImage'

export const COMPOSER_UPLOAD_PATH = '/api/upload/file'

export interface ComposerUploadResult {
  paths: string[]
  /** The server's refusal, or a client-side reading of a bad response. */
  error?: string
  resized: ResizeInfo[]
  resizedByPath: Record<string, ResizeInfo>
}

/** Upload `files` through `post`, which sends the multipart body and returns
 *  the response. A refusal is returned as `error`, not thrown; a thrown
 *  transport error (network, abort) propagates. */
export async function uploadComposerFiles(
  files: File[],
  post: (body: FormData) => Promise<Response>,
): Promise<ComposerUploadResult> {
  // Downscale oversized images client-side so they fit the model's image
  // limits before they ever reach the server (see resizeImage.ts).
  const prepared = await Promise.all(files.map(f => resizeImageForModel(f)))
  const resized = prepared.map(p => p.info).filter((i): i is ResizeInfo => i !== null)
  const fd = new FormData()
  prepared.forEach(p => fd.append('file', p.file))
  const res = await post(fd)
  let body: { paths?: unknown; error?: string }
  try { body = await res.json() } catch { body = {} }
  if (!res.ok) return { paths: [], error: body.error || res.statusText, resized, resizedByPath: {} }
  if (!Array.isArray(body.paths)) return { paths: [], error: i18nT('api.client.unexpected_server_response'), resized, resizedByPath: {} }
  // The server appends one path per multipart 'file' part in order, so
  // paths[i] is prepared[i]'s stored location — zip them to key resize
  // details by the exact server path the attachment chip renders from.
  const paths = body.paths as string[]
  const resizedByPath: Record<string, ResizeInfo> = {}
  prepared.forEach((p, i) => { if (p.info && paths[i]) resizedByPath[paths[i]] = p.info })
  return { ...(body as { paths: string[]; error?: string }), resized, resizedByPath }
}
