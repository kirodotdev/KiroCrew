/**
 * The Content-Security-Policy for the Files app's HTML preview, and the builder
 * that injects it into a previewed document.
 *
 * Split out of `viewers.tsx` deliberately. Every literal here is handed to a
 * PARSER — CSP directives and `<meta>`/`<head>` markup — and none of it is read
 * as words, so it is the same category as `lib/widgetSrcdoc.ts` and
 * `apps/meetings/lib/sketchSrcdoc.ts`. `viewers.tsx` itself holds real user copy
 * (the preview/source toggle labels), so it cannot carry a path exemption; this
 * module can, because it is copy-free: it imports no `i18nT`/`useTranslation`,
 * renders no text node, and exports one pure builder.
 */

/**
 * A deny-by-default policy applied to every previewed document.
 *
 * `sandbox=""` on the iframe already blocks scripts, but it does NOT block
 * PASSIVE subresource loads: an `<img src="https://tracker/px.gif">` in a saved
 * page still fires on preview, which both beacons "this file was opened" to a
 * third party and — with a loopback or private-range URL — probes the user's own
 * network from inside their browser. `default-src 'none'` stops all of it.
 *
 * What stays allowed is only what a self-contained document needs to LOOK right:
 * inline styles (`<style>` blocks and `style=` attributes) and images or fonts
 * already embedded as `data:` URIs. No external stylesheet, font, frame, media,
 * or fetch of any kind is reachable. The `'unsafe-inline'` here is style-only
 * and is not a script vector.
 */
export const PREVIEW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"

/**
 * Put the policy at the very front of the document, where the parser meets it
 * before any content it must govern.
 *
 * Deliberately does NOT search for `<head>`. An earlier revision inserted after
 * the first `/<head\b[^>]*>/i` match, which also matches inside a COMMENT: a
 * document beginning `<!-- <head> --><img src="https://attacker/">` parked the
 * meta inside the comment, where it is inert, and the image then fired — the
 * exact passive load this policy exists to stop, in attacker-controlled preview
 * HTML. Any regex that reads markup without parsing it has that class of hole.
 *
 * So: skip only an ANCHORED leading doctype (keeping it first, or the document
 * silently renders in quirks mode and lays out differently) and put the meta
 * immediately after it. A `<meta>` ahead of `<html>` is hoisted into the head by
 * the parser, so it governs the whole document without needing to find the head.
 */
export function withPreviewCsp(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">`
  // Anchored at the start (leading whitespace only) — never a free search.
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html)
  if (doctype) {
    const at = doctype.index + doctype[0].length
    return html.slice(0, at) + meta + html.slice(at)
  }
  return meta + html
}
