/** Plain-text stand-in used while the Pierre chunk loads and for patch text
 *  that does not (yet) parse — e.g. the partial frames of a streaming diff.
 *
 *  Pierre's own geometry, MEASURED from a rendered block (7 blocks, heights
 *  exactly 50 + 20×lines): 2px border, 32px header, 16px body padding, 20px per
 *  line. The stand-in has to match all of it, or the swap moves the reader.
 *
 *  `leading-5` and `py-2` say 20px and 8px — and inside a transcript they were
 *  BOTH LOSING: `.msg-content pre` (specificity 0,1,1) beats a single-class
 *  utility and sets `line-height:1.5` (19.5px at 13px) with `padding:10px 12px`.
 *  The stand-in therefore rendered 4px taller per surface than the thing it
 *  stands in for, and the reader was displaced by 12-36px per transcript row the
 *  moment the chunk resolved (measured in a browser: -4px on every
 *  `.pierre-surface`, three code blocks in one row = -36px). `pierre-plain` is
 *  the hook that wins that fight; the metrics live in index.css next to the rule
 *  they have to beat. Keep the two geometries equal or the reflow returns.
 *
 *  Also the FINAL render for patch surfaces when the plain-diff preference is
 *  on (see `usePlainDiff`), which is why it accepts the caller's `className`:
 *  in that mode it stands in for the Pierre element the class was written for.
 *  The geometry above still has to hold there — the preference can flip while a
 *  transcript is on screen, so this element replaces a Pierre surface in place.
 */
export function PlainCodeFallback({ text, className }: { text: string; className?: string }) {
  return (
    <pre className={`pierre-plain m-0 px-3 py-2 overflow-x-auto text-[13px] font-mono leading-5 whitespace-pre${className ? ` ${className}` : ''}`}>
      {text}
    </pre>
  )
}
