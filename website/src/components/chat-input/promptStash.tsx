/* The composer's prompt-stash surfaces: the status line and the menu rows.
 * Both are imported eagerly. Together they are a few KB of source and the App
 * chunk has tens of KB of headroom under its budget, so a lazy split would buy
 * nothing and would add a load-failure path (and its fallback UI) to the
 * composer for no gain. The stash logic itself is `hooks/usePromptStash.ts`;
 * `ChatInput.tsx` wires it. */
export { default as PromptStashStatusLine } from '../PromptStashStatusLine'
export { PromptStashMenuItems } from '../PromptStashBadge'
