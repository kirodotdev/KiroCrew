/**
 * A doc's page header (PoC): an optional emoji icon and an optional cover, kept as
 * front matter at the top of the doc's own markdown file, so the file stays plain
 * markdown and the header travels with it:
 *
 *     ---
 *     icon: 🎯
 *     cover: gradient:2
 *     ---
 *
 * The pane splits that block off before the editor sees the text and puts it back
 * on every save; other front matter lines are kept as they were.
 */

export interface DocMeta {
  icon?: string
  /** `gradient:<n>`, `color:<n>`, or an https image address. */
  cover?: string
  /** Minutes between checks of the doc's heading goals, all of them; unset is the default. */
  'goal-every'?: string
}

export interface SplitDoc {
  meta: DocMeta
  /** Front matter lines that are not ours, kept in order. */
  other: string[]
  body: string
}

const FRONT = /^---\n([\s\S]*?)\n---\n?/

export function splitDoc(text: string): SplitDoc {
  const m = FRONT.exec(text)
  if (!m) return { meta: {}, other: [], body: text }
  const meta: DocMeta = {}
  const other: string[] = []
  for (const line of m[1].split('\n')) {
    const kv = /^(icon|cover|goal-every):\s*(.*)$/.exec(line)
    if (kv && kv[2].trim()) meta[kv[1] as keyof DocMeta] = kv[2].trim()
    else if (!kv) other.push(line)
  }
  return { meta, other, body: text.slice(m[0].length) }
}

export function joinDoc({ meta, other, body }: SplitDoc): string {
  const lines = [
    ...(meta.icon ? [`icon: ${meta.icon}`] : []),
    ...(meta.cover ? [`cover: ${meta.cover}`] : []),
    ...(meta['goal-every'] ? [`goal-every: ${meta['goal-every']}`] : []),
    ...other,
  ]
  return lines.length ? `---\n${lines.join('\n')}\n---\n${body}` : body
}

// ---- covers ---------------------------------------------------------------------

/** Gradients and colours from theme tokens, so a cover follows light and dark. */
export const GRADIENTS = [
  'linear-gradient(135deg, var(--accent), var(--info))',
  'linear-gradient(135deg, var(--info), var(--ok))',
  'linear-gradient(135deg, var(--warn), var(--danger))',
  'linear-gradient(135deg, var(--accent), var(--danger))',
  'linear-gradient(135deg, var(--ok), var(--warn))',
  'linear-gradient(160deg, var(--accent-subtle), var(--accent))',
  'linear-gradient(135deg, var(--bg-elevated), var(--border-strong))',
  'radial-gradient(circle at 20% 30%, var(--info), transparent 60%), radial-gradient(circle at 80% 70%, var(--accent), transparent 55%), var(--bg-elevated)',
]

export const COLORS = [
  'color-mix(in srgb, var(--accent) 35%, var(--bg))',
  'color-mix(in srgb, var(--info) 35%, var(--bg))',
  'color-mix(in srgb, var(--ok) 35%, var(--bg))',
  'color-mix(in srgb, var(--warn) 35%, var(--bg))',
  'color-mix(in srgb, var(--danger) 35%, var(--bg))',
  'var(--bg-hover)',
]

export const isImageUrl = (v: string) => /^https:\/\/[^\s"'()<>]+$/.test(v.trim())

/** The CSS background for a stored cover value, or null for one this build cannot draw. */
export function coverBackground(cover: string | undefined): string | null {
  if (!cover) return null
  const g = /^gradient:(\d+)$/.exec(cover)
  if (g) return GRADIENTS[Number(g[1])] ?? null
  const c = /^color:(\d+)$/.exec(cover)
  if (c) return COLORS[Number(c[1])] ?? null
  // Quoted, and only an https address: nothing in the value can leave the url().
  if (isImageUrl(cover)) return `center / cover no-repeat url("${cover.trim()}")`
  return null
}

/** A new cover: one of the gradients, picked at random as Notion does. */
export const randomGradient = (rand = Math.random) => `gradient:${Math.floor(rand() * GRADIENTS.length)}`

/** The icon picker's emoji, a short everyday set. */
export const EMOJI = [
  '📄', '📝', '📌', '📎', '🗂️', '📊', '📈', '🧾', '✅', '☑️', '🎯', '🚀',
  '💡', '🧠', '🔥', '⭐', '🌱', '🌊', '🏔️', '🌙', '☀️', '⚡', '🧭', '🗺️',
  '🛠️', '⚙️', '🧪', '🐛', '🔒', '🔑', '📦', '🏗️', '💬', '📣', '🗓️', '⏱️',
  '🤝', '👥', '🎨', '🎵', '📚', '🏆', '❤️', '💜', '🟣', '🟢', '🟡', '🔴',
]
