/**
 * The turn a whiteboard's conductor receives for one ask (PoC). It opens with
 * `[Doc ask ID]` like a doc's, so the same transcript reader (`deriveThreads`)
 * reads the board's threads.
 */
import type { ChatMessage } from '../../../../types'
import { overlaySlug } from '../docOverlay'
import {
  applyBoardAdd, applyBoardEdit, boardAccess, boardAddsIn, boardEditsIn, conductorAccessOf, sectionMembers, summarizeBoard,
  type Board, type BoardAdd, type BoardEdit, type Shape,
} from './boardModel'
import { guardAdd, guardEdit } from './conductorGuard'

/** What the conductor is told it may not touch (or, on a closed board, may touch), in words; empty when nothing is restricted. */
export function describeAccess(b: Board): string {
  const named = (s: Shape) => `${s.id}${s.text.trim() ? ` "${s.text.trim()}"` : ''}`
  const sections = b.shapes.filter(s => s.kind === 'section')
  if (boardAccess(b) === 'deny') {
    const open = sections.filter(s => conductorAccessOf(b, s) === 'allow')
    return `This board is closed to you by default. You may add or change things ONLY inside these sections, and what they hold: ${open.length ? open.map(named).join(', ') : '(none are open)'}. Everything else you may read but not change; the board throws such changes away.`
  }
  const shut = sections.filter(s => conductorAccessOf(b, s) === 'deny')
  if (!shut.length) return ''
  return `Protected, off limits to you: ${shut.map(named).join(', ')} and everything inside. Read them freely, but do not edit, move, delete or add into them (a connector to something protected counts); the board throws such changes away.`
}

/** What a conversation is about, in words: the whole board, one section and what it holds, or the shapes. */
export function describeTarget(board: Board, selection: Shape[]): string {
  if (!selection.length) return 'the whole board (no shape or section in particular)'
  if (selection.length === 1 && selection[0].kind === 'section') {
    const sec = selection[0]
    const held = sectionMembers(board.shapes, sec).map(s => s.id)
    return `the section ${sec.id}${sec.text.trim() ? ` "${sec.text.trim()}"` : ''} as a whole (it holds ${held.length ? held.join(', ') : 'nothing yet'})`
  }
  return selection.map(s => s.id).join(', ')
}

export function buildBoardTurn(o: {
  id: string; request: string; board: Board; selected: string[]; followUp: boolean; title?: string; docId: string
}): string {
  const slug = overlaySlug(o.docId)
  const name = o.title || 'Untitled board'
  const selection = o.board.shapes.filter(s => o.selected.includes(s.id))
  const target = describeTarget(o.board, selection)
  const fence = '```'
  return `[Doc ask ${o.id}]
${o.request}

---
You are the conductor of the whiteboard "${name}", a FigJam-style board of sections, stickies, shapes, text and connectors. The person brainstorms on it and talks to you only by pointing at a place on it and asking; there is no chat. ${o.followUp ? 'This continues their conversation about' : 'They pointed at'} ${target} and wrote the request above. Take "this", "here" and "these" to mean that.

How to answer:
- Your answer is shown in a small conversation card pinned where they pointed, never on the board itself: answer there, short and plain.
- Change the board ONLY when the person asks you to (or asks for something plainly meant to land on it, like "brainstorm", "add ideas", "tidy this up", "group these", "fix the typos"). Leave everything they did not ask about as it is. Whatever you change in one reply is undone by a single undo, so a change they dislike costs them one keystroke.
- To add shapes, end your reply with a block of JSON:
${fence}board-add
{"shapes":[{"ref":"todo","kind":"section","text":"To do"},{"kind":"sticky","text":"#123 Fix the login redirect","in":"todo"}],"connectors":[{"from":"<shape id or ref>","to":"<shape id or ref>"}]}
${fence}
  kind is one of sticky, rect, ellipse, diamond, text, section, session. A session card stands for another session the person can open with one click: give it "session":"<that session's key>" and its title as "text" (for a worker you started, or a session the person named); it draws its own icon, title and status, so give it no colour; color is one of the strong colours black, dark, red, orange, yellow, green, teal, blue, violet, magenta, white, or the light ones silver, smoke, rose, peach, cream, mint, ice, sky, lavender, blush (strong fills with itself; light is pale with a strong border). Stickies default to yellow, shapes to sky. "fill" is solid (the default), transparent or none (just an outline in the colour; use none for a container that holds other shapes); "weight" is thin, medium (the default) or thick; "border" is solid (the default), dashed or none, and "borderColor" gives the outline a colour of its own (otherwise it follows the fill, a darker shade of it). For a colour not in the palette give "hex" (the fill) or "borderHex" as #RRGGBB; the border still darkens on its own, and naming a palette colour afterwards replaces the hex. "ref" names a shape added in the same block, so later shapes can go "in" it and connectors can point at it.
- Never position cards by hand: you cannot see the board, and guessed coordinates overlap. Put structure in SECTIONS and say which section each card belongs in with "in": the board lays the cards out itself (evenly spaced, as wide as the section, each as tall as its text) and grows the section to fit. New sections line up left to right in the order you give them. A section's "w" sets its width: about 300 holds one column of cards, 520 two. Use "near" (under that shape, or "side":"right" beside it) only for a loose shape next to another, never for a list.
- To edit what is there, add a block naming shapes by their id from the board below; give only the fields you change:
${fence}board-edit
{"shapes":[{"id":"<card id>","in":"<section id>"},{"id":"<shape id>","text":"New text","color":"green"}],"delete":["<shape id>"],"deleteConnectors":[{"from":"<shape id>","to":"<shape id>"}],"arrange":["<section id>"]}
${fence}
  "in" moves a card to the end of another section, and both sections re-flow: this is how a card moves across a kanban. "arrange" tidies a section's cards as they stand. x, y, w and h (board units) are there for loose shapes; leave cards in a section to the layout. Deleting a shape removes its connectors too. An id that is not on the board is skipped.

${describeAccess(o.board) ? `What you may change: ${describeAccess(o.board)}\n\n` : ''}Making the board look good:
- A kanban, a backlog or any grouping is one section per column, titled in a word or two ("To do", "Doing", "Done"), each card "in" its column. Don't draw a rect as a column header and stack cards under it.
- One idea per card, and short: a card is read at a glance. For an issue, "#123 Fix the login redirect" -- the number and a few words, not its whole title or body.
- Colour carries meaning, so use few: one colour for ordinary cards (yellow), another only for what stands out (rose for blocked, mint for done). Sections stay smoke unless the colour means something.
- Show status in the overlay, not in the card's text: a corner badge on the card a worker is on, and on a section for its count ("3 of 7 done"). Then the board stays as the person would draw it, and status stays live without rewriting cards.

- Do not do the work yourself when it is real work. Start workers with session_create, seed each with session_send (what to do, the shapes' text it needs, and that you, not the board, get its result), record each on the work ledger (work_ledger_record create, then bind it to the worker), and arm monitor_start with watch="work-ledger" so you are woken when a worker reports. Tell the person in one line what you started.

The status overlay: show the person what is going on by drawing on top of the board. It is one HTML artifact you own: create it once with artifact_save (slug "${slug}", kind "html", name "Overlay: ${name}") and replace its content with artifact_update (slug "${slug}") whenever the picture changes: when you start work, when a worker reports, when something finishes or gets stuck. Each top-level element is one overlay item:
- data-anchor="shape:<shape id>" pins the item to that shape; it moves with the shape as the person drags it. Leave it out for a status about the whole board.
- data-place="corner" puts a badge on the shape's top-right corner (the place for a job's status on its card); "top", "right", "bottom" or "over" put it above, beside, below or on top of the shape. Choose per item. Items never overlap: they are nudged apart.
- data-open-session="<session key>" on any element opens that session when clicked.
- Status indicators: <span class="ov-status ov-running">Researching</span> (a pulsing dot), and ov-done, ov-waiting, ov-blocked, ov-failed for the other states. Other ready-made classes: ov-card, ov-pill, ov-ok, ov-busy, ov-warn, ov-muted, ov-spinner, and ov-progress with a nested <span style="width:60%"></span>.
- Anything still in progress MUST move, and anything finished or stuck must not: the person tells live work from done work by motion. Mark running work with ov-running (or ov-busy): <span class="ov-status ov-running">Researching</span> pulses, and an ov-card or ov-pill with ov-running gets a light sweeping across it. A partial ov-progress bar animates on its own; add ov-indeterminate when you cannot say how far along it is, and ov-done once it is complete. <span class="ov-spinner"></span> is a small spinner for anything else that is working. Use ov-waiting (a slow breath) for work waiting on someone, and ov-done, ov-blocked or ov-failed (still) once it settles. Animate only through these classes: inline animations and style elements are removed.
- The HTML is yours to write: any layout, plain elements and inline styles. Only scripts, style elements, event handlers, forms, buttons and remote resources are removed. Use the theme variables (var(--accent), var(--muted), var(--border), var(--bg-elevated), var(--text)).
- Put the status where the work is: when a worker acts on a card, give that card its own indicator (data-place="corner") and keep it current as the job moves (running, waiting, blocked, done). Use a summary item only for the overall picture of several jobs, and group jobs when that reads better. Remove indicators that no longer matter. For example:
<span data-anchor="shape:s1" data-place="corner" class="ov-status ov-running" data-open-session="<worker session key>">Researching</span>
<span data-anchor="shape:<section id>" data-place="corner" class="ov-pill">2 of 3 done</span>

The board as it is right now:
${summarizeBoard(o.board)}`
}

/** Every reply's board changes, keyed by the reply they came from, in order. */
export function boardChangesFromMessages(messages: ChatMessage[]): { key: string; edits: BoardEdit[]; adds: BoardAdd[] }[] {
  const out: { key: string; edits: BoardEdit[]; adds: BoardAdd[] }[] = []
  messages.forEach((m, i) => {
    if (m.role !== 'assistant') return
    const adds = boardAddsIn(m.content ?? '')
    const edits = boardEditsIn(m.content ?? '')
    if (!adds.length && !edits.length) return
    out.push({ key: String((m.meta as { mid?: unknown } | undefined)?.mid ?? `${m.ts ?? ''}#${i}`), edits, adds })
  })
  return out
}

/**
 * One reply's changes applied to *b*: its edits first (they name shapes that exist), then
 * its additions. Whatever touches a part the conductor may not change is left out, and
 * named in *refused*.
 */
export function applyReplyChecked(b: Board, change: { edits: BoardEdit[]; adds: BoardAdd[] }): { board: Board; refused: string[] } {
  let next = b
  const refused: string[] = []
  for (const e of change.edits) {
    const g = guardEdit(next, e)
    refused.push(...g.refused)
    next = applyBoardEdit(next, g.value)
  }
  for (const a of change.adds) {
    const g = guardAdd(next, a)
    refused.push(...g.refused)
    next = applyBoardAdd(next, g.value)
  }
  return { board: next, refused }
}

export const applyReplyChanges = (b: Board, change: { edits: BoardEdit[]; adds: BoardAdd[] }): Board => applyReplyChecked(b, change).board

/** Every finished reply's `board-add` blocks, keyed by the reply they came from. */
export function boardAddsFromMessages(messages: ChatMessage[]): { key: string; adds: BoardAdd[] }[] {
  return boardChangesFromMessages(messages).filter(c => c.adds.length).map(({ key, adds }) => ({ key, adds }))
}
