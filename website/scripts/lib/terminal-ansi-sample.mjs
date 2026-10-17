/**
 * The ANSI byte sample the terminal-palette capture feeds through its scripted
 * PTY. Every colour is written as a plain ANSI SGR code — nothing here names a
 * hex value, which is the point: whatever appears on screen came from the
 * terminal's palette, not from this file.
 */

const RESET = '\x1b[0m'
/** ANSI 30-37 are the eight base colours; 90-97 the bright half. */
const NAMES = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white']

/** One swatch row: a filled block per entry, plus its name. */
function swatchRow(offset, label) {
  return `${label} ` + NAMES
    .map((n, i) => `\x1b[${offset + i}m\u2588\u2588${RESET} ${n.padEnd(8)}`)
    .join('')
}

/**
 * A prompt on ANSI background colours, a `git diff` fragment on red/green, and
 * the sixteen swatches. The swatch rows are the load-bearing part: with the
 * palette unset, xterm paints them from its own built-in colours.
 */
export function ansiSample() {
  return [
    // The powerline separator glyph is deliberately absent: the capture browser
    // has no Nerd Font, so it would render as tofu and put a missing-glyph
    // question in front of a reviewer who is looking at colours.
    `${RESET}\x1b[44;30m ~/repos/kirocrew ${RESET}\x1b[43;30m  main ± ${RESET}`,
    '',
    `\x1b[1mdiff --git a/CliPanel.tsx b/CliPanel.tsx${RESET}`,
    `\x1b[36m@@ -21,7 +21,9 @@${RESET}`,
    `\x1b[31m-    selectionBackground: read('--accent-subtle'),${RESET}`,
    `\x1b[32m+    selectionBackground: read('--accent-subtle'),${RESET}`,
    `\x1b[32m+    ...ansiPaletteFromVars(read),${RESET}`,
    '',
    swatchRow(30, 'ansi   '),
    swatchRow(90, 'bright '),
    '',
  ].join('\r\n')
}
