/**
 * Candidate monospace families probed against the viewing machine's font book,
 * and the sample text the picker measures and previews with.
 *
 * The browser can only answer "is family X installed?" (see `fontDetect.ts`) — it
 * cannot list the font book without the permission-gated Local Font Access API.
 * So the no-permission path needs a list of names to ask about, and a family
 * absent from this list is simply never offered; the picker keeps free text so an
 * unlisted family is still reachable by typing it.
 *
 * Scope is deliberately monospace-only: xterm lays glyphs on a fixed grid, so a
 * proportional family renders a terminal with misaligned columns.
 *
 * FONT FAMILY NAMES ONLY. Every string here is matched by value against the
 * machine's font book — a translated name resolves to nothing and the terminal
 * silently falls back — which is why this module is exempt from the untranslated
 * gate in `eslint.i18n.config.js`. Any interface copy for the picker belongs in
 * the catalog, not behind that exemption.
 */

/**
 * Families shipped with an OS or installed directly under their own name.
 *
 * Ordered by how likely a terminal user is to have one, because the picker
 * shows them in this order and the first screenful should carry the common
 * cases rather than an alphabetical accident.
 */
const PLAIN_MONO_FAMILIES = [
  // Popular coding fonts, installed by name.
  'JetBrains Mono',
  'JetBrains Mono NL',
  'Fira Code',
  'Fira Mono',
  'Cascadia Code',
  'Cascadia Mono',
  'Source Code Pro',
  'IBM Plex Mono',
  'Hack',
  'Iosevka',
  'Iosevka Term',
  'Inconsolata',
  'Roboto Mono',
  'Ubuntu Mono',
  'Ubuntu Sans Mono',
  'Anonymous Pro',
  'Space Mono',
  'Victor Mono',
  'Maple Mono',
  'Maple Mono NF',
  'Geist Mono',
  'Commit Mono',
  'Departure Mono',
  '0xProto',
  'Monaspace Neon',
  'Monaspace Argon',
  'Monaspace Xenon',
  'Intel One Mono',
  'Recursive Mono Linear Static',
  'Nimbus Mono PS',
  // Paid/licensed families common among terminal users.
  'Berkeley Mono',
  'Comic Code',
  'Dank Mono',
  'Operator Mono',
  'PragmataPro',
  'MonoLisa',
  // macOS.
  'SF Mono',
  'Menlo',
  'Monaco',
  'Andale Mono',
  'Courier New',
  'PT Mono',
  // Windows.
  'Consolas',
  'Lucida Console',
  'Cascadia Code PL',
  'Cascadia Mono PL',
  // Linux distributions.
  'DejaVu Sans Mono',
  'Liberation Mono',
  'Noto Sans Mono',
  'FreeMono',
  'Terminus',
  'Fixed',
  // CJK-capable monospace, so a CJK terminal user is not forced into fallback.
  'Sarasa Mono SC',
  'Sarasa Term SC',
  'Noto Sans Mono CJK SC',
  'Source Han Mono',
  'MS Gothic',
  'Hiragino Kaku Gothic Pro',
]

/**
 * Base names of Nerd Font patched builds.
 *
 * A patched build installs under a family name derived from the base — the
 * unsuffixed name is the variable-width-icon build, ` Mono` narrows every glyph
 * to one cell, and ` Propo` is proportional (offered anyway: it is what some
 * users have installed, and xterm will still render it).
 */
const NERD_FONT_BASES = [
  'JetBrainsMono',
  'FiraCode',
  'FiraMono',
  'CaskaydiaCove',
  'CaskaydiaMono',
  'Hack',
  'SauceCodePro',
  'BlexMono',
  'Iosevka',
  'IosevkaTerm',
  'UbuntuMono',
  'UbuntuSansMono',
  'DejaVuSansMono',
  'LiberationMono',
  'Inconsolata',
  'RobotoMono',
  'Meslo',
  'MesloLG',
  'VictorMono',
  'AnonymicePro',
  'Terminess',
  '0xProto',
  'GeistMono',
  'CommitMono',
  'MartianMono',
  'ZedMono',
  'ComicShannsMono',
  'Monaspice',
  'Noto',
  'SpaceMono',
  'Hasklug',
  'Lilex',
  'ProggyClean',
]

/**
 * Family names the Nerd Font installers register that do not follow the
 * `<base> Nerd Font` pattern — chiefly the `NF` short form (what the Windows
 * and Homebrew casks of some releases produce) and powerlevel10k's recommended
 * build, whose family name carries no "Nerd Font" at all.
 */
const NERD_FONT_ALIASES = [
  'MesloLGS NF',
  'MesloLGM NF',
  'MesloLGL NF',
  'JetBrainsMono NF',
  'JetBrainsMonoNL NF',
  'FiraCode NF',
  'Hack NF',
  'CaskaydiaCove NF',
  'SauceCodePro NF',
  'UbuntuMono NF',
  'DejaVuSansMono NF',
  'Terminess NF',
]

/** Suffixes a Nerd Font base installs under, widest cell first. */
const NERD_FONT_SUFFIXES = ['Nerd Font', 'Nerd Font Mono', 'Nerd Font Propo']

/**
 * Every family name worth probing, deduplicated and in display order.
 *
 * Plain families lead because an exact-name install is the common case; the
 * generated Nerd Font permutations follow, and the aliases close the list.
 */
export const MONO_FONT_CANDIDATES: readonly string[] = Array.from(
  new Set([
    ...PLAIN_MONO_FAMILIES,
    ...NERD_FONT_BASES.flatMap(base => NERD_FONT_SUFFIXES.map(sfx => `${base} ${sfx}`)),
    ...NERD_FONT_ALIASES,
  ]),
)

/**
 * Probe string for the installed-font measurement.
 *
 * Mixes the widest and narrowest ASCII glyphs with digits, long enough that a
 * per-glyph difference of a fraction of a pixel accumulates past floating-point
 * noise. Repeated runs rather than a pangram because the comparison is about
 * advance widths, not glyph coverage.
 */
export const FONT_PROBE_TEXT = 'mmmmmmmmmmlliiiiWWWWWW0123456789@#'
