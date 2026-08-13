import { KIND_LABEL } from './constants'
import type { Report, Screen, DiscoveryScreen } from './types'

/**
 * The critic persona and method, carried by the prompt rather than by an agent.
 *
 * A bundled `design-critic` agent cannot be used from a builtin: a builtin
 * manifest may declare `agents`, but discovery never serializes that field and
 * `bridges.py` resolves an app root under the user data dir, where a builtin has
 * only `app.json`. The agent is therefore never registered, and naming it on
 * `/api/chat` fails every request. (`dev_fleet` has the same latent gap with its
 * two declared skills, neither of which is registered today.) So the persona,
 * the voice rules and the method live here and run on the core agent.
 */
const CRITIC =
  'You are an experienced designer running a heuristic design critique — a fellow designer ' +
  'looking over someone’s work, not a title on a review panel.\n\n' +
  'Before judging anything, load the method. Resolve the directory that holds it and read ' +
  'both files from it:\n' +
  '  python3 -c "import kiro_crew, pathlib; print(pathlib.Path(kiro_crew.__file__).parent / \'apps/builtins/design_critique/skills/design-critique\')"\n' +
  'If that prints nothing or python3 is unavailable, try `python` instead, and if neither ' +
  'works use glob to find a path ending in ' +
  '`apps/builtins/design_critique/skills/design-critique/SKILL.md`. From that directory ' +
  'read SKILL.md and frameworks/main-checklist.md with fs_read, then follow their method ' +
  'exactly. Do this first, every time — without the checklist the critique is guesswork.\n\n' +
  'Voice: lead with a one-line overall read and a health tally using the NN/g severity names ' +
  '(Cosmetic / Minor / Major / Catastrophe), then what is working, then the top 3-5 things ' +
  'you would tighten (element → problem → fix), then one line on what the evidence could not ' +
  'show. Positives before fixes. No composite 0-100 score. Be warm, specific and concrete. ' +
  'Do not invent personas or backstories to justify a finding. Never judge what the supplied ' +
  'evidence cannot reveal, and never critique visuals from unrendered source.\n\n'

// As a BUILTIN app, the skill's scripts live inside the installed kiro_crew
// package, not under ~/.kiro/crew/apps. The path is machine-specific, so we never
// hardcode it: instead every prompt that runs a script tells the agent to resolve
// the two directories itself and substitute the printed paths wherever <SCRIPTS>
// and <UPLOADS> appear. <SCRIPTS> is the skill's bundled scripts dir; <UPLOADS> is
// the KiroCrew uploads dir (both derived with pathlib, no absolute paths baked in).
const RESOLVE_PATHS =
  'FIRST, resolve two directories on THIS machine and use the printed paths wherever ' +
  '<SCRIPTS> and <UPLOADS> appear below. Run each command and read its single line of output:\n' +
  '  <SCRIPTS> = the design-critique skill scripts dir:\n' +
  '    python3 -c "import kiro_crew, pathlib; print(pathlib.Path(kiro_crew.__file__).parent / \'apps/builtins/design_critique/skills/design-critique/scripts\')"\n' +
  '  <UPLOADS> = the KiroCrew uploads dir:\n' +
  // Must go through config_dir(), not Path.home(): KIROCREW_HOME can move the
  // data home (a dev instance does exactly that), and hardcoding ~/.kiro/crew
  // would write captures into the production home and contaminate isolated data.
  // This is the same resolution the upload handler itself uses.
  '    python3 -c "from kiro_crew.config.paths import config_dir; print(config_dir() / \'uploads\')"\n\n'

// Shared tail: the JSON contract. Same shape for one screen or many.
export const SCHEMA = (multi: boolean): string =>
  'Return ONLY JSON (no prose, no code fences) matching exactly:\n' +
  '{"overallRead":string,"health":string,' +
  '"tally":{"catastrophe":int,"major":int,"minor":int,"cosmetic":int},' +
  '"screens":[{"step":int,"label":string,"path":string}],' +
  '"findings":[{"severity":"cosmetic|minor|major|catastrophe","title":string,' +
  '"category":string,"scope":"screen|flow","steps":[int],"location":string,' +
  '"evidence":string,"fix":string,"rules":[string],' +
  '"box":{"x":number,"y":number,"w":number,"h":number}}],' +
  '"keep":[string],"couldNotSee":[string]}\n\n' +
  '"screens" lists every screen you actually saw, in order, with the absolute image path ' +
  'for each. "box" is the APPROXIMATE region of the issue within ITS screen as fractions 0-1 ' +
  '(x,y = top-left, w,h = size); use null if you cannot localize it.\n\n' +
  (multi
    ? 'Set "scope" to "screen" for a finding that lives on one screen (put that one step in ' +
      '"steps", and give a box). Set "scope" to "flow" for a problem that only exists because ' +
      'this is a sequence — inconsistency between steps, no progress indicator, no way back, ' +
      'repeated asks, dead ends. List every step it involves in "steps" and use null for "box". ' +
      'Count a cross-screen problem once, not once per screen.\n\n'
    : 'Use "scope":"screen" and "steps":[1] for every finding.\n\n') +
  'In "rules", name the 1-3 design principles or heuristics the finding rests on ' +
  '(e.g. “Nielsen: consistency”, “Gestalt: proximity”, “WCAG 1.4.3 contrast”).\n\n' +
  'Phrase "fix" as a suggestion (“Consider…”, “One option…”, “You might…”), not a command, ' +
  'for every finding EXCEPT accessibility — accessibility fixes may be stated directly.\n\n' +
  'Only include findings for what you actually saw. List anything you could not see under ' +
  '"couldNotSee" instead of guessing.'

export const IMAGES_PROMPT = (paths: string[], brief?: string): string => {
  const multi = paths.length > 1
  return (
    CRITIC +
    (multi
      ? 'Please critique this flow of ' + paths.length + ' screens, in the order given. ' +
        'Run your design-critique skill in FLOW MODE: walk each step in order (what is this ' +
        'screen asking the user to do, is the next action obvious, what happens between this ' +
        'screen and the next, where is the friction), then check the jumps between steps. ' +
        'Do not narrate what each screen contains.'
      : 'Please run a design critique on this screenshot.') +
    (brief ? ' Context: ' + brief + '.' : '') + '\n\n' +
    SCHEMA(multi) + '\n\n' +
    'The screens, in order:\n' +
    paths.map((p, i) => (multi ? 'Step ' + (i + 1) + ':\n' : '') + '![screen](' + p + ')').join('\n\n') + '\n\n' +
    'For "screens", use these exact paths in this order: ' + JSON.stringify(paths) + '. ' +
    'Give each a label of ONE or TWO plain words naming the screen (e.g. "Cart", "Shipping", ' +
    '"Payment", "Confirmation"). No parentheses, no state descriptions, max 18 characters.'
  )
}

// Figma / repo / local code / live URL: the critic has to produce the pixels itself
// before judging them, using the skill's bundled scripts.
// STEP 1 — find the candidate screens. No critique yet. The same chat slot is
// reused for step 2, so a cloned repo stays on disk between the two calls.
export const DISCOVER_PROMPT = (kind: string, value: string): string => {
  const how: Record<string, string> = {
    repo: 'Clone it shallow into a temp dir with credential prompts DISABLED so it can never ' +
      'hang: `GIT_TERMINAL_PROMPT=0 git clone --depth 1 <url> <dir>`. If the clone fails, do NOT ' +
      'continue — return "blocked" with reason "no-access" and the git error verbatim in detail. ' +
      '(GitHub says "Repository not found" both for a repo that does not exist and for a private ' +
      'one you cannot read, so do not guess which; say both are possible.) On success, run ' +
      '`node <SCRIPTS>/discover-routes.mjs <dir>` and read its JSON. Keep the temp dir — ' +
      'you will render from it next. To judge canSee, ALSO try ' +
      '`node <SCRIPTS>/capture-build.mjs <dir> --routes=/a,/b` — it serves any ALREADY-BUILT ' +
      'output (dist/build/out/storybook-static) over loopback http and renders it, which is the only ' +
      'way a built SPA can be seen. If it reports usableForVisualCritique:false with a blockedBy gate, ' +
      'put that in "cannotSee" and set every affected screen canSee:false. Do NOT install dependencies ' +
      'and do NOT start a dev server.',
    local: 'Run `node <SCRIPTS>/discover-routes.mjs <dir>` on that path and read its JSON. To ' +
      'judge canSee, ALSO try `node <SCRIPTS>/capture-build.mjs <dir> --routes=/a,/b` — it serves ' +
      'already-built output over loopback http and renders it. A local checkout often HAS a build even ' +
      'when the repo does not (dist is usually gitignored), so try it. If it reports ' +
      'usableForVisualCritique:false with a blockedBy gate, put that in "cannotSee" and set the ' +
      'affected screens canSee:false. Do NOT install dependencies and do NOT start a dev server.',
    figma: 'List the top-level frames/pages using the Figma desktop MCP tools. Each frame is a ' +
      'candidate screen. If you cannot reach it, return "blocked" and say WHICH of these it is, ' +
      'because the fix differs: the Figma desktop app is not running / those MCP tools are not ' +
      'available to you at all / the app is running but that file is not open / the file opened but ' +
      'your account cannot view it. Never lump these together as "cannot access".',
    url: 'This page is ALREADY being served — do NOT start any server. Capture it with ' +
      '`node <SCRIPTS>/capture-site.mjs` (or render.mjs for the single page). Then list the ' +
      'same-origin links you ACTUALLY FOUND in the page as further candidate screens. If the URL ' +
      'does not respond, say so in "cannotSee" — do not try to start it.',
  }
  const needsScripts = kind === 'repo' || kind === 'local' || kind === 'url'
  return (
    CRITIC +
    (needsScripts ? RESOLVE_PATHS : '') +
    'Do NOT critique anything yet. I need to know what screens are IN this ' +
    (KIND_LABEL[kind] || 'design') + ' so the user can choose what to audit.\n\n' +
    'Target: ' + value + '\n\n' + (how[kind] || '') + '\n\n' +
    'Then return ONLY JSON (no prose, no code fences):\n' +
    '{"framework":string,"note":string,' +
    '"blocked":{"reason":"no-access|not-found|figma-app-missing|figma-file-closed|figma-no-permission|other","detail":string}|null,' +
    '"screens":[{"id":string,"label":string,"ref":string,"group":string,"canSee":boolean,"why":string}],' +
    '"flows":[{"label":string,"why":string,"basis":"observed|guess","screenIds":[string]}],' +
    '"cannotSee":[string]}\n\n' +
    '"screens": every candidate screen. "id" is a short slug you invent. "label" is ONE or TWO ' +
    'plain words (max 18 chars, no parentheses). "ref" is the route path, file, or URL to render. ' +
    '"group" buckets it loosely (e.g. "auth", "checkout", "settings", "marketing"). ' +
    '"canSee" is true ONLY if you could actually render it to a PNG — a served URL usually can; ' +
    'from code, static HTML and self-contained pages usually can, while a React/Vue app that needs ' +
    'a build or a server usually cannot. "why" is a short reason when canSee is false.\n\n' +
    '"flows": sequences that belong together, ordered the way a user would move through them. ' +
    'Set "basis" to "observed" ONLY when you saw the actual links/navigation connecting them ' +
    '(possible on a served URL). Otherwise "guess" — inferring from route names and folder ' +
    'structure — and say what the guess rests on in "why". Omit if nothing groups.\n\n' +
    '"note": one plain sentence on what can and cannot be seen here.\n\n' +
    'If you could not get in AT ALL, set "blocked" (with the underlying error in "detail") and ' +
    'return zero screens — that is a different situation from getting in and finding nothing ' +
    'renderable, and the user is told a different thing in each case.\n\n' +
    'Be honest: it is fine to return zero screens with an explanation in "cannotSee".'
  )
}

// STEP 2 — critique only the screens the user picked, in their order.
export const SCOPED_PROMPT = (picks: DiscoveryScreen[], brief?: string): string =>
  CRITIC +
  RESOLVE_PATHS +
  'Now critique exactly these screens, in this order' + (picks.length > 1 ? ' as one flow' : '') + ':\n' +
  picks.map((p, i) => (i + 1) + '. ' + p.label + ' — ' + (p.ref || p.id)).join('\n') + '\n\n' +
  (brief ? 'Context from the user: ' + brief + '\n\n' : '') +
  'Render each one to a PNG. For a built app use `node <SCRIPTS>/capture-build.mjs <dir> ' +
  '--routes=<comma-separated> --out=<UPLOADS>`; for a single file or URL use ' +
  '`node <SCRIPTS>/render.mjs <file-or-url> <out.png>`; for Figma export the frame. Save into ' +
  '<UPLOADS> with unique filenames. If render.mjs exits 5 (BLANK PAGE) or capture-build reports ' +
  'usableForVisualCritique:false, that screen was NOT seen — put it in "couldNotSee" and ask for ' +
  'screenshots or a Figma link instead. Still no dependency installs ' +
  'and no dev server. Critique ONLY what you actually saw; anything you could not render goes in ' +
  '"couldNotSee" — never critique it from source.\n\n' +
  (picks.length > 1
    ? 'Run FLOW MODE from the skill: walk each step in order, then check the jumps between them.\n\n'
    : '') +
  'Ignore the screens the user did not pick.\n\n' + SCHEMA(picks.length > 1)

// Seeds a fresh slot with the critique so follow-up answers are grounded in the
// actual report rather than guessed. Sent once per session of questions.
export const ASK_CONTEXT = (rep: Report, screens: Screen[]): string =>
  'Context — you already produced this critique. Do NOT re-critique anything; the user ' +
  'just wants to understand parts of it.\n\n' +
  'Overall read: ' + (rep.overallRead || '') + '\n' +
  'Screens: ' + (screens || []).map((s, i) => (i + 1) + '. ' + (s.label || 'Screen')).join(', ') + '\n\n' +
  'Findings:\n' + (rep.findings || []).map((f, i) =>
    (i + 1) + '. [' + f.severity + '] ' + f.title +
    (f.evidence ? ' — evidence: ' + f.evidence : '') +
    (f.fix ? ' — suggested: ' + f.fix : '') +
    (f.rules && f.rules.length ? ' — based on: ' + f.rules.join('; ') : '')
  ).join('\n') + '\n\n' +
  'Reply "ready" and nothing else.'

export const ASK_PROMPT = (quote: string, question?: string): string =>
  'The user highlighted this from your critique:\n\n“' + quote + '”\n\n' +
  'Their question: ' + (question || 'What does this mean?') + '\n\n' +
  'Answer in 2-4 plain sentences, as a designer explaining to another designer. Explain the ' +
  'reasoning or the principle behind it and what they would actually change. No headings, no ' +
  'bullet lists, no restating the question. If the honest answer is that you are not sure or the ' +
  'evidence did not show it, say that plainly.'
