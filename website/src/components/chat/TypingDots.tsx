/**
 * TypingDots — three bouncing dots that mean "a reply is being typed", drawn
 * inside the crewmate chat's typing bubble (`CrewmateLiveActivity`).
 *
 * Decorative only: the host supplies the words (the caption), so the dots are
 * `aria-hidden`. Colour is `currentColor`, so the host paints them by setting
 * its own text colour. Motion is `.typing-dots` in index.css; the global
 * reduced-motion rule ends it on its first frame, which is the resting style.
 */
export default function TypingDots() {
  return (
    <span className="typing-dots" aria-hidden="true">
      <span /><span /><span />
    </span>
  )
}
