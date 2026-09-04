/** Shared diff parsing utilities used by ToolInputText and DiffBlock. */

/** Rewrite a patch's file-header paths down to their basenames.
 *
 *  Pierre titles a file header from the paths inside the patch and renders the
 *  title `direction: rtl` with an ellipsis, so a deep path reads as
 *  `…/components/DiffBlock.tsx`. In chat the basename alone identifies the file,
 *  and the full path stays reachable: callers extract it from the UNTOUCHED
 *  patch, so the Open button still probes and opens the real path.
 *
 *  Only column-0 header lines are rewritten. A unified diff always prefixes
 *  body lines (`+`, `-`, or a space), so content can never occupy column 0 and
 *  be mistaken for a header. `/dev/null` is left alone — it marks an added or
 *  deleted side rather than naming a file. */
export function basenamePatchHeaders(patch: string): string {
  const short = (tok: string): string => {
    if (tok === '/dev/null') return tok
    // `a/` and `b/` are git's own side markers, not directories: keep them so
    // the rewritten header still parses as the same kind of patch.
    const m = /^([ab]\/)(.*)$/.exec(tok)
    const prefix = m ? m[1] : ''
    const rest = m ? m[2] : tok
    return prefix + rest.slice(rest.lastIndexOf('/') + 1)
  }
  return patch.split('\n').map(line => {
    const file = /^(--- |\+\+\+ )(.*)$/.exec(line)
    if (file) {
      // Some generators append a tab-separated timestamp after the path.
      const [path, ...tail] = file[2].split('\t')
      return file[1] + [short(path), ...tail].join('\t')
    }
    const git = /^diff --git (\S+) (\S+)$/.exec(line)
    return git ? `diff --git ${short(git[1])} ${short(git[2])}` : line
  }).join('\n')
}

/** Detect whether text contains unified diff content.
 *  Requires @@ hunk headers or paired ---/+++ file headers to avoid
 *  false positives on markdown lists, negative numbers, and CLI flags.
 *  Note: YAML front matter (---) + markdown +++ headings could false-positive,
 *  but this is unlikely in tool input context where content is code/JSON. */
export function isDiffText(text: string): boolean {
  const lines = text.split('\n')
  return lines.some(l => /^@@\s/.test(l)) ||
    (lines.some(l => /^--- /.test(l)) && lines.some(l => /^\+\+\+ /.test(l)))
}
