/** Plain-text stand-in used while the Pierre chunk loads and for patch text
 *  that does not (yet) parse — e.g. the partial frames of a streaming diff.
 *  Text content matches the final render, so the swap is a restyle, not a
 *  content reflow. */
export function PlainCodeFallback({ text }: { text: string }) {
  return (
    <pre className="m-0 px-3 py-2 overflow-x-auto text-[13px] font-mono leading-relaxed whitespace-pre">
      {text}
    </pre>
  )
}
