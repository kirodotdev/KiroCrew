/**
 * The composer upload body (`POST /api/upload/file`), shared by every surface
 * that stages composer attachments: the dashboard client and the ChatEmbed
 * composer. Only the transport differs -- the dashboard calls `fetch`, an app
 * calls its scoped client -- so the caller passes the POST and this owns the
 * rest: client-side image downscaling, the multipart body, the `paths`
 * contract and pairing each resize with the server path it landed at.
 */
import { i18nT } from '../i18n/t'
import { HeicPrepareError, prepareHeicImage } from './heicImage'
import { resizeImageForModel, type ResizeInfo, type ResizeResult } from './resizeImage'

/** One file as it will be uploaded: a HEIC/HEIF photo becomes a JPEG sized to
 *  the model's limits, any other oversized raster is downscaled, and
 *  everything else passes through. */
async function prepareComposerFile(file: File): Promise<ResizeResult> {
  const heic = await prepareHeicImage(file)
  return heic ?? resizeImageForModel(file)
}

export const COMPOSER_UPLOAD_PATH = '/api/upload/file'

export interface ComposerUploadResult {
  paths: string[]
  /** The server's refusal, or a client-side reading of a bad response. */
  error?: string
  resized: ResizeInfo[]
  resizedByPath: Record<string, ResizeInfo>
}

/** The rejection a cancelled upload raises: an `AbortError`, the name every
 *  caller already reads as "the user cancelled", not as a failure. */
function cancelled(): DOMException {
  return new DOMException('The upload was cancelled.', 'AbortError')
}

/** `work`, abandoned the moment `signal` aborts. A HEIC decode cannot be
 *  interrupted part-way, so it finishes unobserved and its result is dropped;
 *  what matters is that Cancel ends the upload at once instead of after it. */
function untilAborted<T>(work: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return work
  if (signal.aborted) return Promise.reject(cancelled())
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(cancelled())
    signal.addEventListener('abort', onAbort, { once: true })
    work.then(
      value => { signal.removeEventListener('abort', onAbort); resolve(value) },
      err => { signal.removeEventListener('abort', onAbort); reject(err) },
    )
  })
}

/** Upload `files` through `post`, which sends the multipart body and returns
 *  the response. A refusal is returned as `error`, not thrown; a thrown
 *  transport error (network, abort) propagates. `signal` is the upload's
 *  cancel: once it aborts, preparation stops and an `AbortError` is thrown
 *  before anything is posted. */
export async function uploadComposerFiles(
  files: File[],
  post: (body: FormData) => Promise<Response>,
  signal?: AbortSignal,
): Promise<ComposerUploadResult> {
  // Convert HEIC photos to JPEG and downscale oversized images client-side,
  // so what reaches the server already fits the model's image limits (see
  // heicImage.ts and resizeImage.ts). One file at a time: a full-size photo
  // decodes to tens of megabytes of pixels, and twenty at once would not fit.
  // A photo that cannot be converted refuses the whole batch before anything
  // is sent, as a server refusal of any one file does.
  const prepared: ResizeResult[] = []
  for (const f of files) {
    // Checked before the next preparation starts, not only while one runs:
    // a cancelled upload must not begin decoding the photo after it.
    if (signal?.aborted) throw cancelled()
    try {
      prepared.push(await untilAborted(prepareComposerFile(f), signal))
    } catch (err) {
      if (err instanceof HeicPrepareError) return { paths: [], error: err.message, resized: [], resizedByPath: {} }
      throw err
    }
  }
  if (signal?.aborted) throw cancelled()
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
