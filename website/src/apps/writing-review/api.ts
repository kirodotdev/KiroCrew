// HTTP client for the Writing Review backend. Every route is same-origin
// under /api/apps/writing-review and rides the dashboard's session cookie.
import type {
  JobStatus,
  ReviewContextBundle,
  ReviewDetail,
  ReviewsListResponse,
  ScanJobResponse,
  ScanRequest,
  Settings,
} from './lib/types'

const API_BASE = '/api/apps/writing-review'

interface ApiErrorBody {
  error?: string
  code?: string
}

/** Error surface for backend calls; carries the machine-readable code
 *  when the server provided one. */
export class WritingReviewApiError extends Error {
  code: string

  constructor(message: string, code = '') {
    super(message)
    this.name = 'WritingReviewApiError'
    this.code = code
  }
}

async function parseErrorBody(response: Response): Promise<WritingReviewApiError> {
  try {
    const body = (await response.json()) as ApiErrorBody
    return new WritingReviewApiError(body.error || `HTTP ${response.status}`, body.code || '')
  } catch {
    return new WritingReviewApiError(`HTTP ${response.status}`)
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' })
  if (!response.ok) throw await parseErrorBody(response)
  return response.json() as Promise<T>
}

async function sendJson<T>(
  path: string,
  method: 'POST' | 'PATCH' | 'DELETE',
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) throw await parseErrorBody(response)
  return response.json() as Promise<T>
}

/** Response shape for the ``/uploads`` endpoint. */
interface DocumentUploadResponse {
  doc_path: string
  doc_name: string
}

/** POST a ``File`` to the binary upload endpoint via multipart form-data.
 *
 * Used for browsed ``.docx`` (and any other file we cannot safely route
 * through ``FileReader.readAsText``). The backend writes the bytes
 * unchanged into the uploads dir and returns a ``doc_path`` the
 * subsequent scan submit uses in the ``doc_path`` field. Text files
 * (``.md`` / ``.txt``) still go through the text-in-JSON path in
 * ``handleBrowseFileSelected`` because the readAsText round-trip is
 * lossless for UTF-8 text and skipping a second HTTP round-trip keeps
 * the browse UX snappier for the common case.
 */
async function uploadBinary(browsedFile: File): Promise<DocumentUploadResponse> {
  const formData = new FormData()
  formData.append('file', browsedFile, browsedFile.name)
  const response = await fetch(`${API_BASE}/uploads`, {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
  })
  if (!response.ok) throw await parseErrorBody(response)
  return response.json() as Promise<DocumentUploadResponse>
}

export const writingReviewApi = {
  listReviews: () => getJson<ReviewsListResponse>('/reviews'),

  startScan: (payload: ScanRequest) =>
    sendJson<ScanJobResponse>('/scan', 'POST', payload),

  uploadDocumentFile: (browsedFile: File) => uploadBinary(browsedFile),

  getJob: (jobId: string) =>
    getJson<JobStatus>(`/jobs/${encodeURIComponent(jobId)}`),

  listJobs: (status?: string) =>
    getJson<{ jobs: JobStatus[] }>(
      status ? `/jobs?status=${encodeURIComponent(status)}` : '/jobs',
    ),

  getReview: (reviewId: string) =>
    getJson<ReviewDetail>(`/reviews/${encodeURIComponent(reviewId)}`),

  getReviewContext: (reviewId: string) =>
    getJson<ReviewContextBundle>(`/reviews/${encodeURIComponent(reviewId)}/context`),

  deleteReview: (reviewId: string) =>
    sendJson<{ deleted: boolean }>(`/reviews/${encodeURIComponent(reviewId)}`, 'DELETE'),

  getSettings: () => getJson<Settings>('/settings'),

  updateSettings: (patch: Partial<Settings>) =>
    sendJson<Settings>('/settings', 'PATCH', patch),
}
