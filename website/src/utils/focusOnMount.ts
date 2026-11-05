/**
 * Callback ref that focuses its element once, when the element mounts.
 *
 * Pass it as `ref={focusOnMount}`. It is one function for the whole app, so
 * React calls it only when the element attaches or detaches. An inline
 * `ref={el => el?.focus()}` is a NEW function on every render, so React calls
 * it again on every render: any state change (ticking a checkbox, a refetch)
 * pulls focus back to that element and breaks the user's Tab position.
 */
export function focusOnMount(el: HTMLElement | null): void {
  el?.focus()
}
