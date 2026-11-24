/**
 * The Bot's brief for its 3D dashboard: section 8 of the `dashboard` skill, made from the layout's own rules in
 * `dash3d.ts` (the field, the kinds, each limit and unit, the lamp states, the words of the write's reply) and the
 * room's own why lines. The skill
 * carries this text as it is, and `dashBrief.test.ts` fails when the two differ, or when a limit the brief names is
 * not where `readLayout` holds it.
 */
import en from '../../../i18n/locales/en.json'
import { DASH, DASH_FIELD, DASH_KINDS, DASH_SAYS, DASH_UNITS, LAMP_STATES, NO_VALUE, isPart, partSays, readLayout, type DashPart, type DashWhy } from './dash3d'

const KEY_MAX = DASH.keyMax

/** Every reason a layout or a part is not drawn, in the order the reader asks its questions. */
export const DASH_WHYS: readonly DashWhy[] = ['not_layout', 'version', 'no_parts', 'too_many', 'part', 'kind', 'name', 'field', 'path', 'max', 'key']

/** The small whole layout the brief shows: every part of it is drawn (the unit reads it). */
export const BRIEF_EXAMPLE = {
  v: DASH.version,
  parts: [
    { kind: 'ring', name: 'Checks', field: 'checks_done', max: 20 },
    { kind: 'bar', name: 'Spend', field: 'spend_today', max: 'spend_budget', unit: '$' },
    { kind: 'lamps', name: 'Workers', field: 'workers' },
    { kind: 'number', name: 'Open PRs', field: 'open_prs' },
  ],
} as const

/** Why a part shows a dash, in the words of the write's reply, in the order the reader asks. */
export const DASH_DASHES: readonly string[] = [DASH_SAYS.noValue, DASH_SAYS.noPath, DASH_SAYS.notList, DASH_SAYS.notNumber, DASH_SAYS.noLimit]

/** The reply lines the brief quotes: made by the reader itself from the example, so they are lines a Bot will get. */
export function briefReplies(): { layout: string; number: string } {
  const [ring, bar] = readLayout(BRIEF_EXAMPLE).parts.filter(isPart) as DashPart[]
  const wrong = (en.xr as Record<string, string>).dash3d_why_name
  return {
    layout: `In the XR room this layout reads: part 1: ${partSays(ring, { checks_done: 12 })}; part 2: ${partSays(bar, { spend_today: 'soon', spend_budget: 40 })}; part 3: ${wrong}.`,
    number: `In the XR room: part 1: ${partSays(ring, { checks_done: 13 })}.`,
  }
}

const code = (s: string) => `\`${s}\``
const list = (xs: readonly string[]) => xs.map(code).join(', ')
const whyLine = (w: DashWhy): string => (en.xr as Record<string, string>)[`dash3d_why_${w}`].replace(/^layout: /, '')

/** The section, as the skill carries it. */
export function dashBrief(): string {
  const [number, bar, ring, lamps] = DASH_KINDS
  const replies = briefReplies()
  const example = `{"v": ${BRIEF_EXAMPLE.v}, "parts": [\n${BRIEF_EXAMPLE.parts.map(p => `  ${JSON.stringify(p).replace(/,"/g, ', "').replace(/":/g, '": ')}`).join(',\n')}\n]}`
  return `## 8. In XR: a 3D dashboard

In the XR room of pages a Bot can carry a small solid dashboard: a row of
parts on a tray that it holds in front of its body, and that the person
takes to read. The room builds it from ONE field of your own page,
${code(DASH_FIELD)}, and from the fields that field names. Your flat page
stays as it is.

Add one only when the person asks for a 3D dashboard or uses the XR room.
A Bot on a shipped template page keeps that page: never replace it with a
page of your own for this (section 1).

1. Your page in force declares ${code(DASH_FIELD)} as an \`object\` field, and
   every field a part reads. On a page of your own, add each one with
   \`dashboard_add_field\` (\`name\`, \`type\`): it saves a draft of your
   whole page with that field added and changes nothing else. Add all you
   need, then \`dashboard_apply\` once. A field with choices, or one read
   from a fold, goes into the model you \`dashboard_save\`. A Bot on a
   shipped page has no 3D dashboard.
2. Write the layout once with \`dashboard_write\`, \`field: "${DASH_FIELD}"\`.
   It names fields and holds no number itself. The reply ends with one
   line that says what the XR room makes of the value it stored, and
   what each part shows now:
   "${replies.layout}"
   A part named "name not checked" holds a sign this gateway cannot
   check; a grey tray says why it is empty; "no 3D dashboard" after a
   clear. A wrong layout is stored all the same: fix the part it names
   and write it again.
3. Keep the numbers true by writing their own fields, as for the flat page.
   The reply to such a write ends with the same words for the parts that
   read that field: "${replies.number}"
4. To take it away: \`dashboard_write\`, \`field: "${DASH_FIELD}"\`, \`clear: true\`.

The layout is \`{"v": ${DASH.version}, "parts": [...]}\` with 1 to ${DASH.mostParts} parts, drawn left
to right in the order written. A key the room does not know is not read.

| Key of a part | What it takes |
|---|---|
| \`kind\` | one of ${list(DASH_KINDS)} |
| \`name\` | the word under the part: 1 to ${DASH.nameMax} letters; a letter keeps at most ${DASH.marksMost} combining marks, and control and format characters are removed |
| \`field\` | the field of your page it shows: a lowercase letter, then lowercase letters, digits or \`_\`, 64 characters at most |
| \`path\` | optional, for an object field: one string of dotted keys, at most ${DASH.pathKeys}, each of letters, digits or \`_\`, ${KEY_MAX} characters at most |
| \`max\` | ${code(bar)} and ${code(ring)} only, and needed there: a number above 0, or the name of a field that holds one |
| \`key\` | ${code(lamps)} only, optional: the key of each row that holds its state (\`state\` when left out): letters, digits or \`_\`, ${KEY_MAX} characters at most |
| \`unit\` | optional: one of ${list(DASH_UNITS)}; any other is dropped |

| Kind | Its field holds | It shows |
|---|---|---|
| ${code(number)} | a number, or text that is a plain number | the number, in at most ${DASH.numberMax} characters, with its unit |
| ${code(bar)} | a number, or text that is a plain number | a column filled to its share of \`max\`, up to ${DASH.overShare} times; the number with its unit |
| ${code(ring)} | a number, or text that is a plain number | a ring filled to its share of \`max\`, written as \`n/max\`, with no unit |
| ${code(lamps)} | a list of rows (or of words) | one lamp a row, at most ${DASH.mostLamps}, the rest only counted: ${list(LAMP_STATES)}; any other word is grey |

A part whose field is missing, or holds what its kind does not take (a word
for a number, a number for lamps), stands with a dash (${NO_VALUE}) for its
number, and the room says no more. The reply's line says why:
${DASH_DASHES.map(d => `"${d}"`).join(', ')}.
A number in that line is the number the part reads, written plainly;
the room writes it short. A text that a field holds is never repeated.

A small whole layout:

\`\`\`json
${example}
\`\`\`

\`dashboard_write\` checks the value as the object your page declares, and
stores a wrong layout all the same. The reply's last line is how you
learn what the room makes of it: read it after every write. The room
draws a document that is no layout as an empty grey tray, and a wrong
part as an empty socket while the others stand. Taken, each says why in
one line: ${DASH_WHYS.map(w => `"${whyLine(w)}"`).join(', ')}.
`
}
