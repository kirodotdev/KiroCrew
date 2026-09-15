// Shared types for the Design Tweak (design-tweak) builtin page.
//
// The backend returns loosely-shaped JSON; these interfaces mark every field
// the UI actually reads so the page can be strict (no implicit any) without
// pretending the payloads are exhaustively known.

export interface SelectionElement {
  tag?: string
  id?: string
  classes?: string[]
  locator?: string
}

export interface EditSelection {
  mode?: string
  elements?: SelectionElement[]
}

export interface ThreadEntry {
  role: string
  text: string
  ts?: number
  status?: string
}

export interface Comment {
  cid: string
  index: number
  status: string
  comment: string
  createdAt?: string
  selection?: EditSelection
  previewUrl?: string
  projectId?: string
  projectRoot?: string
  sourceFile?: string
  followUpTo?: string
  followUpLabel?: string
  element?: string
  count?: number
  locator?: string
  parentLocator?: string
  point?: unknown
  thread?: ThreadEntry[]
  devServer?: string
}

// A request = one "visual_edit_batch" of comments handed to the agent together.
export interface Request {
  id: string
  number: number
  status?: string
  state?: string
  projectId?: string
  projectRoot?: string
  createdAt?: string
  sentAt?: string
  /** Set once the panel confirmed the prompt reached the agent. Sealed with no
   *  `deliveredAt` is the stranded state the retry bar covers. */
  deliveredAt?: string
  thread?: ThreadEntry[]
  comments?: Comment[]
}

export interface Project {
  id: string
  path: string
  name?: string
  previewUrl?: string
  /** 'static' when previewUrl is the app's loopback static server (served from
   *  disk), absent when it is a dev server. Lets the UI keep saying "dev server"
   *  only where that is actually true. */
  previewMode?: 'static'
  devUrl?: string
  needsDevServer?: boolean
  devCommand?: string
  unbundledEntry?: string
}

// Minimal shape both a Request and a Comment satisfy — used by the
// "does this belong to the previewed project?" test.
export interface PreviewScoped {
  projectId?: string
  projectRoot?: string
  previewUrl?: string
}

// ── Overlay <-> panel postMessage payloads ───────────────────────────────────

export interface CapturePayload {
  type?: string
  comment: string
  selection?: EditSelection
  followUpTo?: string
  projectId?: string
  previewUrl?: string
  [key: string]: unknown
}

export interface OverlayMessage {
  source?: string
  type?: string
  payload?: CapturePayload
  clientRef?: string
  id?: string
  text?: string
}

// ── Backend API response shapes ──────────────────────────────────────────────

export interface ProjectsResponse {
  projects?: Project[]
  activeId?: string
  serving?: boolean
}

export interface QueueResponse {
  pending?: Request[]
}

/**
 * `GET /health`. `dataDir` is the data home the backend actually resolved
 * (`KIROCREW_APP_DATA_DIR` / `KIROCREW_HOME` dependent), and is the only correct
 * source for the payload path the page quotes to the agent.
 */
export interface HealthResponse {
  status?: string
  app?: string
  version?: string
  pending?: number
  dataDir?: string
}

export interface HistoryResponse {
  history?: Request[]
}

export interface AddProjectResponse {
  ok?: boolean
  project?: Project
  updated?: string
  existing?: boolean
  autoDetected?: boolean
  detected?: unknown[]
  error?: string
}

export interface PickFolderResponse {
  ok?: boolean
  path?: string
  canceled?: boolean
  error?: string
}

export interface SelfUpdateResponse {
  ok?: boolean
  version?: string
  note?: string
  error?: string
}

export interface SendResponse {
  ok?: boolean
  error?: string
  /** The SEALED snapshot. The prompt is built from this rather than from the
   *  client's copy, so a comment that joined the draft just before the seal
   *  cannot end up sealed-but-undispatched. */
  request?: Request
  /** True when the backend found the request ALREADY sealed, so this caller did
   *  not perform the seal. The atomic cut has exactly one winner; a loser that
   *  dispatched anyway would hand the agent a second copy of the same batch. */
  already?: boolean
}

export interface SubmitResponse {
  ok?: boolean
  cid?: string
  label?: string
  id?: string
  number?: number
  commentCount?: number
  error?: string
}

export interface RemoveResponse {
  ok?: boolean
  error?: string
}

export interface SimpleResponse {
  ok?: boolean
  error?: string
}

export interface PreviewUrlResponse {
  error?: string
}

export interface DevServerCandidate {
  url: string
  port: number | string
}

export interface DetectDevServerResponse {
  suggested?: string
  candidates?: DevServerCandidate[]
}

export interface DevServerStartResponse {
  ok?: boolean
  error?: string
  devUrl?: string
  url?: string
  adopted?: boolean
}

export interface DeleteCommentResponse {
  error?: string
}

// Host chat API (slot creation) response.
/**
 * `POST /api/chat/slots` — the ADOPT response (`serialize_slot`). Note that
 * `messages` here is a COUNT, not the transcript, and there is no `queue`: the
 * host returns `len(self.messages)` on this path. Read the transcript from
 * `GET /api/chat/slots/{key}` (`SlotTranscript`) instead.
 */
export interface ChatSlotResponse {
  key?: string
  messages?: number
}

/**
 * `GET /api/chat/slots/{key}` — prepared message entries plus anything accepted
 * but not yet processed. Used to verify that a sealed batch actually reached the
 * agent's session — see `delivery.ts`.
 */
export interface SlotTranscript {
  messages?: unknown[]
  queue?: unknown[]
  /**
   * The server returned only the most recent window (both `resume` and the
   * detail GET cap at 200 rows) and OLDER rows exist that were not sent.
   *
   * Load-bearing for delivery verification, not informational: a request older
   * than the window is absent from `messages` while having been delivered, and
   * reading that absence as "not delivered" resends it, applying every edit in
   * the batch a second time. With this flag the verdict can be `unknown`
   * instead of `missing`, which is the honest answer and the one that does not
   * corrupt anything.
   */
  hasMore?: boolean
}
