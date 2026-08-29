// TypeScript types mirroring the writing-review backend schema. Kept in a
// single file so the API client and every component share the same shape.

export type Verdict = 'red' | 'yellow' | 'green'
export type Severity = 'high' | 'medium' | 'low' | 'advisory'
export type Confidence = 'high' | 'medium' | 'low'
export type CrossValidation = 'clean' | 'conflicts' | 'redundant'

export interface Finding {
  id: string
  scanner: string
  section: string
  paragraph: number
  issue: string
  rule: string
  severity: Severity
  proposed_fix: string
  cross_validation: CrossValidation
  conflicts: string[]
  confidence: Confidence
  // Set by the cross-validation pass when this finding is tagged
  // ``redundant`` -- points at the id of the finding it duplicates.
  // The frontend does not consume this directly; it is here so the
  // discussion agent's context payload can trace collated groups.
  primary_id?: string
  // Populated by the backend collation step. When one or more redundant
  // findings collapsed onto this primary, each duplicate's location is
  // appended here. The card renders an "Also appears in" list from this
  // array. Absent / empty on non-primary findings.
  related_locations?: RelatedLocation[]
}

export interface RelatedLocation {
  section: string
  paragraph: number
  scanner: string
  issue: string
}

export interface FailedScanner {
  name: string
  reason_class: string
  message: string
  at: string
  duration_ms: number
}

export interface LogReference {
  path: string
  search_hint: string
}

export interface ReviewContext {
  audience: string
  doc_type: string
  tone: string
  additional_context: string[]
  // Free-form directive the author supplied in the New Review dialog:
  // what decision they want the reviewer to focus on. Always a string
  // (defaults to "") — the backend threads a non-empty value into every
  // scanner prompt as a directive line and includes it in the discussion
  // agent's context bundle.
  ask: string
}

export interface ReviewSummary {
  id: string
  doc_name: string
  verdict: Verdict
  finding_count: number
  scanners_run: string[]
  created_at: number
}

export interface ReviewDetail extends ReviewSummary {
  doc_path: string
  context: ReviewContext
  findings: Finding[]
  partial_failure: boolean
  failed_scanners: FailedScanner[]
  log_reference: LogReference | null
  artifact_slug?: string
}

export interface ReviewContextBundle {
  review: ReviewDetail
  document_content: string
  scanner_brief_dir: string
}

export interface ScanRequest {
  doc_path?: string
  doc_text?: string
  // Original filename for browse-uploaded docs. Backend sanitises it and
  // uses it for the review record's display name so the user sees
  // ``hapi_design_doc.md`` in the sidebar instead of the uuid storage key
  // (``abc12345_pasted.md``). Omit for raw-paste — the backend then falls
  // back to its ``pasted document`` label.
  doc_name?: string
  context: ReviewContext
  scanner_toggles?: Record<string, boolean>
}

export interface ScanJobResponse {
  job_id: string
}

export interface JobStatus {
  id: string
  status: 'running' | 'done' | 'failed' | 'interrupted'
  phase: string
  detail?: Record<string, unknown>
  review_id?: string | null
  error?: string | null
  updated_at?: number
  doc_name?: string | null
}

export interface Settings {
  default_audience: string
  default_doc_type: string
  default_tone: string
  scanner_toggles: Record<string, boolean>
  max_concurrent: number
}

export interface ReviewsListResponse {
  reviews: ReviewSummary[]
}
