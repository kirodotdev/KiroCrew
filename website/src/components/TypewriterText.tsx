import { useState, useEffect, useRef } from 'react'

interface Props {
  text: string
  speed?: number
  className?: string
  title?: string
  onDoubleClick?: () => void
}

export default function TypewriterText({ text, speed = 30, className, title, onDoubleClick }: Props) {
  const [displayed, setDisplayed] = useState(text)
  const prevRef = useRef(text)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    const prev = prevRef.current
    prevRef.current = text

    // Only animate if the text actually changed to something different
    // and the previous value looked like a slot key (contains "chat-")
    if (text === prev || !prev.includes('chat-') || text.includes('chat-')) {
      setDisplayed(text)
      return
    }

    // Animate: type out the new title character by character
    let i = 0
    setDisplayed('')
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = window.setInterval(() => {
      i++
      setDisplayed(text.slice(0, i))
      if (i >= text.length) {
        if (timerRef.current) clearInterval(timerRef.current)
        timerRef.current = null
      }
    }, speed)

    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [text, speed])

  // Presentational text label; onDoubleClick is an optional mouse-only
  // enhancement layered over a keyboard-reachable parent control (the real
  // interactive target). A role/tabIndex here would misrepresent the label as
  // a primary control, so the a11y affordance stays on the parent.
  // eslint-disable-next-line jsx-a11y/no-static-element-interactions
  return <span className={className} title={title} onDoubleClick={onDoubleClick}>{displayed}</span>
}
