/**
 * The "design a theme with AI" prompt the Paste CSS tab copies. Model-facing
 * text only, per the `*.prompt.ts` convention in `eslint.i18n.config.js`: the
 * user pastes it into an AI chat alongside a screenshot, so it is English by
 * design and carries no UI copy.
 *
 * The reply shape it asks for is exactly what `parseThemeCss` reads: `.dark` and
 * `.light` blocks of allowlisted variables, with the theme's name and emoji in
 * leading comments the parser lifts into the editor's fields. Change one, check
 * the other — `themeDesignPrompt.test.ts` parses the prompt's own example reply
 * and asserts every listed variable is one the theme allowlist accepts.
 */

/** Every variable the prompt asks for, grouped, with what it paints. */
const THEME_PROMPT_GROUPS: { title: string; vars: [string, string][] }[] = [
  {
    title: 'Surfaces',
    vars: [
      ['--bg', 'main app background'],
      ['--bg-accent', 'barely tinted alternate background (sidebar, stripes), a few % off --bg'],
      ['--bg-elevated', 'raised surfaces: inputs, menus, popovers'],
      ['--bg-hover', 'hover fill for rows and buttons; visible but quiet'],
      ['--card', 'cards and message bubbles'],
      ['--card-fg', 'text on --card'],
      ['--card-hl', 'translucent highlight laid over a card, rgba()'],
      ['--panel', 'side panel background, usually equal to --bg'],
      ['--panel-strong', 'emphasised panel areas and panel headers'],
      ['--chrome', 'top bar; --bg at about .95 alpha, rgba()'],
    ],
  },
  {
    title: 'Text',
    vars: [
      ['--text', 'body text'],
      ['--text-strong', 'headings and emphasised text'],
      ['--muted', 'secondary text, timestamps, labels'],
      ['--muted-strong', 'tertiary text and resting icons; quieter than --muted'],
      ['--muted-fg', 'text drawn on a --muted fill'],
    ],
  },
  {
    title: 'Borders',
    vars: [
      ['--border', 'hairlines and card outlines'],
      ['--border-strong', 'dividers and input outlines'],
      ['--border-hover', 'outline of a hovered control'],
    ],
  },
  {
    title: 'Accent',
    vars: [
      ['--accent', 'brand colour: primary buttons, links, active tab, selection'],
      ['--accent-fg', 'text on an --accent fill'],
      ['--accent-hover', 'hovered --accent'],
      ['--accent-subtle', 'translucent accent tint for selected rows, rgba()'],
      ['--accent-glow', 'translucent accent glow, rgba()'],
      ['--ring', 'keyboard focus ring'],
    ],
  },
  {
    title: 'Status',
    vars: [
      ['--ok', 'success; stays recognisably green'],
      ['--ok-fg', 'text on an --ok fill'],
      ['--ok-subtle', 'translucent success tint, rgba()'],
      ['--warn', 'warning; stays recognisably amber or yellow'],
      ['--warn-fg', 'text on a --warn fill'],
      ['--warn-subtle', 'translucent warning tint, rgba()'],
      ['--danger', 'errors and destructive actions; stays recognisably red'],
      ['--danger-fg', 'text on a --danger fill'],
      ['--danger-subtle', 'translucent danger tint, rgba()'],
      ['--info', 'informational; cyan or blue'],
      ['--info-fg', 'text on an --info fill'],
      ['--aim', 'AI and agent highlights; violet by default, may follow the palette'],
      ['--aim-fg', 'text on an --aim fill'],
      ['--aim-subtle', 'translucent --aim tint, rgba()'],
      ['--clarify', 'an agent question waiting for the user; amber family'],
      ['--clarify-subtle', 'translucent --clarify tint, rgba()'],
    ],
  },
  {
    title: 'Code, diffs and terminal',
    vars: [
      ['--diff-add', 'added-line background, translucent green rgba()'],
      ['--diff-add-text', 'added-line text'],
      ['--diff-del', 'removed-line background, translucent red rgba()'],
      ['--diff-del-text', 'removed-line text'],
      ['--diff-hunk', 'hunk header background, translucent rgba()'],
      ['--diff-hunk-text', 'hunk header text'],
      ['--diff-meta-text', 'file header text in a diff'],
      ['--json-key', 'JSON keys'],
      ['--json-str', 'JSON strings'],
      ['--json-num', 'JSON numbers'],
      ['--json-bool', 'JSON booleans and null'],
      ['--term-magenta', 'terminal ANSI magenta'],
      ['--term-cyan', 'terminal ANSI cyan'],
    ],
  },
  {
    title: 'Shadows',
    vars: [
      ['--shadow-sm', 'box-shadow for small raised controls'],
      ['--shadow-md', 'box-shadow for menus and popovers'],
      ['--shadow-lg', 'box-shadow for dialogs'],
    ],
  },
]

/** A well-formed reply, shown to the model as the shape to copy. */
const EXAMPLE_REPLY = [
  '/* name: Harbor Dusk */',
  '/* emoji: ⚓ */',
  '.dark {',
  '  --bg: #10151c;',
  '  --text: #dfe6ee;',
  '  --accent: #4fb3c8;',
  '  --accent-subtle: rgba(79, 179, 200, .16);',
  '  --shadow-md: 0 4px 12px rgba(0, 0, 0, .35);',
  '  /* …every other variable… */',
  '}',
  '',
  '.light {',
  '  --bg: #f6f8fa;',
  '  --text: #2a3440;',
  '  --accent: #1f7f94;',
  '  --accent-subtle: rgba(31, 127, 148, .12);',
  '  --shadow-md: 0 4px 12px rgba(0, 0, 0, .08);',
  '  /* …every other variable… */',
  '}',
].join('\n')

/** The prompt itself. */
export function themeDesignPrompt(): string {
  const table = THEME_PROMPT_GROUPS
    .map(g => `${g.title}\n${g.vars.map(([name, what]) => `  ${name} — ${what}`).join('\n')}`)
    .join('\n\n')

  return `You are a UI colour designer. Design a colour theme for Kiro Crew, a web dashboard for running and supervising AI agent sessions: a chat transcript, a sidebar of sessions, cards, menus, code diffs and a terminal.

Use the attached screenshot as inspiration. Take its palette, mood and contrast, not its layout. If I attached no image, ask me what look I want before answering.

Reply with ONE css code block and nothing else: no explanation before or after it. Use exactly this shape:

\`\`\`css
${EXAMPLE_REPLY}
\`\`\`

Rules
1. Start with the two comments: a short, evocative theme name (60 characters at most) and one emoji that fits it.
2. Give both blocks. Each block declares EVERY variable listed below, once.
3. .dark is the dark-mode variant and .light the light-mode variant of the SAME theme: keep the hues and character, invert the lightness.
4. Values may only be #hex, rgb()/rgba(), hsl()/hsla(), or for the shadows lengths plus those colours. Never use var(), url(), gradients, quotes, named fonts or !important. Keep each value under 200 characters.
5. Contrast (WCAG): --text and --card-fg at least 7:1 on --bg and --card; --muted at least 4.5:1 on --bg; every *-fg at least 4.5:1 on its own fill; --accent, --ok, --warn, --danger, --info and --ring at least 3:1 against --bg.
6. Surfaces step gently away from --bg toward --text in this order: --bg, --bg-accent, --bg-elevated / --card / --panel-strong, --bg-hover. Borders sit between --bg-hover and --muted.
7. Every variable described as translucent is an rgba() tint of its base colour at roughly .08 to .2 alpha (.95 for --chrome).
8. Status colours keep their meaning at a glance. Shift them toward the palette, but never so far that success, warning and danger can be confused.

Variables

${table}
`
}
