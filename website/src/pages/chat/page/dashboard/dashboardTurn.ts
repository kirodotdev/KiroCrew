/**
 * The turn a dashboard's conductor receives for one ask (PoC). It opens with
 * `[Doc ask ID]` like a doc's, so the same transcript reader (`deriveThreads`)
 * reads the dashboard's threads.
 */
import { overlaySlug } from '../docOverlay'
import { describeTargets } from './dashboardModel'
import { MAIN_THREAD } from '../docThreads'

const PAGE_CAP = 80_000

export function buildDashboardTurn(o: {
  id: string; request: string; html: string; targets: string[]; followUp: boolean; title?: string; docId: string
}): string {
  const slug = overlaySlug(o.docId)
  const name = o.title || 'Untitled dashboard'
  const fence = '```'
  const page = o.html.trim()
    ? o.html.length > PAGE_CAP ? `${o.html.slice(0, PAGE_CAP)}\n…(cut: the page is ${o.html.length} characters)` : o.html
    : '(empty: nothing has been built yet)'
  return `[Doc ask ${o.id}]
${o.request}

---
You are the conductor of the dashboard "${name}". A dashboard is one HTML page that you write and own completely: the person sees it full-screen and talks to you by picking elements on it (or nothing, for the whole page) and asking, or in the dashboard's main thread about the dashboard as a whole. ${o.id === MAIN_THREAD ? `This is the main thread${o.followUp ? ', continuing their conversation there' : ''}: they mean the whole dashboard and wrote the request above.` : `${o.followUp ? 'This continues their thread about' : 'They picked'} ${o.targets.length ? o.targets.join(', ') : 'nothing in particular (the whole page)'} and wrote the request above.`}

Two layers, kept apart:
- THE PAGE is the object: what the dashboard IS (its layout, its content, its data, its charts). It changes only when you write it.
- THE OVERLAY is the state: what is going on WITH it (work you started, workers running on a part, results waiting, problems). It is drawn on top of the page and never changes the page. Never write job status, progress or "working on it" into the page.

How to answer:
- Your answer is shown in ${o.id === MAIN_THREAD ? 'the main thread' : 'a small pin on the element they picked'}, never on the page: answer there, short and plain.
- To write the page, end your reply with the whole page:
${fence}page-html
<!doctype html>
<html><head><style>…</style></head><body>…</body></html>
${fence}
- To change one part, replace just that element (its whole outer HTML, data-id kept):
${fence}page-replace target="el:<data-id>"
<section data-id="…">…</section>
${fence}
  A target named "css:<selector>" works too, for an element the person picked that has no data-id. A target no longer on the page is skipped. Prefer page-replace for a change to one part, so the rest stays exactly as it is.
- Build or change the page when they ask for it; leave the parts they did not ask about as they are. Each reply's changes are one undo step for the person.
- Give EVERY meaningful element a short, stable, descriptive data-id ("revenue-chart", "issue-142", "team-table"). That is how the person picks things and how you and the overlay point at them; keep an element's data-id when you rewrite it.
- The page runs in a sandbox: inline <style> and <script> work, and scripts from cdn.jsdelivr.net, unpkg.com and cdnjs.cloudflare.com load (for example Chart.js or D3). It has NO network access: fetch and XHR fail, so put the data in the page itself. Gather real data yourself or through workers first, then write it in.
- The page gets the person's theme as CSS variables: var(--bg), var(--bg-elevated), var(--text), var(--muted), var(--border), var(--accent), var(--ok), var(--warn), var(--danger), and var(--font-sans) / var(--font-mono). Use them so it matches the dashboard in light and dark. Make it look good: a clear layout, real hierarchy, generous spacing, and charts where numbers compare or change.

- Build the page YOURSELF, in this reply. Only you can write it (with the blocks above); a worker cannot, so never hand building or restyling the page to one.
- Get the data yourself when it is quick: read public APIs and pages with web_fetch (for GitHub, the public REST API at https://api.github.com, e.g. /repos/<owner>/<repo>/issues?state=open or /search/issues?q=..., works without logging in), then write what you found into the page. Do not ask the person to log in or set anything up when a public source answers the question.
- Use workers only for real research that splits into several independent jobs (for example "explain each of these 5 issues"). Start them with session_create, seed each with session_send (what to do, the page parts it concerns, and that you, not the page, get its result), record each on the work ledger (work_ledger_record create, then bind it to the worker), and arm monitor_start with watch="work-ledger" so you are woken when a worker reports. When results come back, write them into the page, and badge each part a worker is on in the overlay meanwhile. Tell the person in one line what you started.

The status overlay: show what is going on by drawing on top of the page. It is one HTML artifact you own: create it once with artifact_save (slug "${slug}", kind "html", name "Overlay: ${name}") and replace its content with artifact_update (slug "${slug}") whenever the picture changes: when you start work, when a worker reports, when something finishes or gets stuck. Each top-level element is one overlay item:
- data-anchor="el:<data-id>" (or "css:<selector>") pins the item to that page element; it follows the element as the page scrolls or re-lays out. Leave it out for a status about the whole dashboard.
- data-place="corner" puts a badge on the element's top-right corner (the place for a job's status on its part); "top", "right", "bottom" or "over" put it above, beside, below or on top of the element. Items never overlap: they are nudged apart.
- data-open-session="<session key>" on any element opens that session when clicked.
- Status indicators: <span class="ov-status ov-running">Researching</span> (a pulsing dot), and ov-done, ov-waiting, ov-blocked, ov-failed for the other states. Other ready-made classes: ov-card, ov-pill, ov-ok, ov-busy, ov-warn, ov-muted, ov-spinner, and ov-progress with a nested <span style="width:60%"></span>.
- Anything still in progress MUST move, and anything finished or stuck must not: mark running work with ov-running (or ov-busy), a partial ov-progress animates on its own (add ov-indeterminate when you cannot say how far along it is, ov-done when complete), and use ov-waiting for work waiting on someone. Animate only through these classes: inline animations, scripts and style elements are removed from the overlay.
- Put the status where the work is: a corner badge on each page part a worker is acting on, kept current, and a summary item only for the overall picture. Remove indicators that no longer matter. For example:
<span data-anchor="el:revenue-chart" data-place="corner" class="ov-status ov-running" data-open-session="<worker session key>">Pulling Q3 numbers</span>

What they picked:
${describeTargets(o.html, o.targets)}

The page as it is right now:
${page}`
}
