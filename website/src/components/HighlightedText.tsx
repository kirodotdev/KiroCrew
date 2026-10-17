import { useSearchHighlight, useCurrentOcc } from '../hooks/SearchHighlightContext'
import { highlightText } from '../utils/highlightText'

export default function HighlightedText({ text }: { text: string }) {
  const { term, caseSensitive } = useSearchHighlight()
  const currentOcc = useCurrentOcc()
  if (!term) return <>{text}</>
  return <>{highlightText(text, term, caseSensitive, currentOcc)}</>
}
