/**
 * What a stream page does when the gateway refuses it as one too many (`too_many`): the warm rooms may close one of
 * theirs to make room (`useWarmRooms.ts`, D48), and then the page is asked for again. One hook for the tab: the
 * workspace that holds warm rooms sets it, and clears it when it goes.
 */
import type { PageSource } from './source'

let hook: ((source: PageSource) => boolean) | null = null

/** Set (or clear) what frees room for a refused page. It returns whether room was freed, so that the page asks again. */
export function setFullHook(fn: ((source: PageSource) => boolean) | null): void { hook = fn }

/** A page was refused as one too many: true when room was freed for it. */
export function freeRoomFor(source: PageSource): boolean { return hook?.(source) ?? false }
