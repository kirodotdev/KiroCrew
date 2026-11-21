/**
 * The read behind a file link's hover peek: which lines to ask for around a
 * `path:line` reference, the one `/api/file-read` request that fetches them
 * (the line window from #40480), and the bound on how many such reads may be
 * in flight at once.
 *
 * Nothing here touches the DOM; `filePeek.tsx` owns the panel.
 */
import { fileReadUrl } from '../../utils/fileReadUrl'
import { FILE_READ_STALE_MS } from '../../utils/fileReadQuery'

/** How many lines the peek shows. The referenced line sits a third of the way
 *  down, so a reader sees what leads to it and what follows. */
export const FILE_PEEK_LINES = 12

/** Lines kept above the referenced line, when the file has them. */
const LINES_BEFORE = Math.floor(FILE_PEEK_LINES / 3)

/** How many peek reads may be in flight at once. A pointer rests on one link at
 *  a time, so one read is the ordinary case; the second is for a pointer that
 *  moves on before the first answers. Past the bound a read is refused here,
 *  before any request, so a message full of links cannot fan out into reads. */
export const FILE_PEEK_MAX_IN_FLIGHT = 2

/** How long a window stays fresh: the same bound the viewer's own read of the
 *  file uses, so a hover and the click that follows it see the same bytes. */
export const FILE_PEEK_STALE_MS = FILE_READ_STALE_MS

/** The 1-based, inclusive line range a peek asks for. */
export interface FilePeekWindow { start: number; end: number }

/** What the peek shows for one window. `ok` false means the link gets no
 *  panel: a refused or failed read, a binary file, or a gateway that answered
 *  the whole file because it does not know the window parameters yet. */
export interface FilePeekRead {
  ok: boolean
  /** The lines the body holds, one entry per line, without their newlines. */
  lines: string[]
  /** The line number of `lines[0]`, from the gateway's `X-Line-Start`. */
  start: number
}

const NO_PEEK: FilePeekRead = { ok: false, lines: [], start: 0 }

/** The window around `line`: `FILE_PEEK_LINES` lines with `line` a third of
 *  the way down, pulled up to start at 1 near the top of a file. The gateway
 *  clamps the end to the file, so a short file simply answers fewer lines. */
export function filePeekWindow(line: number): FilePeekWindow {
  const start = Math.max(1, Math.floor(line) - LINES_BEFORE)
  return { start, end: start + FILE_PEEK_LINES - 1 }
}

/** The react-query key of one window of one file. */
export function filePeekQueryKey(path: string, range: FilePeekWindow): readonly [string, string, number, number] {
  return ['file-peek', path, range.start, range.end]
}

let inFlight = 0

/** Thrown, not returned, when the in-flight bound refuses a read: react-query
 *  keeps an error as stale, so the next hover asks again once the bound frees,
 *  where a returned `ok: false` would sit in the cache for `FILE_PEEK_STALE_MS`. */
export class FilePeekBusy extends Error {
  constructor() { super('file peek: too many reads in flight') }
}

/**
 * Read `range` of `path` through `GET /api/file-read?path=…&start=…&end=…`.
 * The gateway applies its owner gate, path validation, sensitive-path refusal,
 * read cap and credential pass exactly as for a full read, then cuts the
 * window from that body and names what came back in `X-Line-Start` /
 * `X-Line-End`. Any answer other than a 2xx text body with those headers is
 * `ok: false`: the link keeps its ordinary hover, and a hover never raises a
 * prompt or shows a file the click could not open.
 */
export async function fetchFilePeek(path: string, range: FilePeekWindow, signal?: AbortSignal): Promise<FilePeekRead> {
  if (inFlight >= FILE_PEEK_MAX_IN_FLIGHT) throw new FilePeekBusy()
  inFlight += 1
  try {
    const url = `${fileReadUrl(path)}&start=${range.start}&end=${range.end}`
    const res = await fetch(url, signal ? { signal } : undefined)
    if (!res.ok || res.headers.get('X-File-Binary') === 'true') return NO_PEEK
    // A gateway without the window (an older build) answers the whole capped
    // file and no `X-Line-Start`. Showing that body would peek at the whole
    // file after all, so it shows nothing.
    const start = Number(res.headers.get('X-Line-Start'))
    if (!Number.isInteger(start) || start < 1) return NO_PEEK
    const text = await res.text()
    const lines = text.split('\n')
    if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
    return { ok: true, lines, start }
  } finally {
    inFlight -= 1
  }
}

/** For tests: how many peek reads are in flight right now. */
export function __filePeekInFlight(): number {
  return inFlight
}
