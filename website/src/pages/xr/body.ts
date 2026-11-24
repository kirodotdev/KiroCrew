/**
 * The words the scene uses for a Bot's body: the state a Bot is in, and the pose its body holds.
 *
 * The scene drives a `BotDrive` (`botDrive.ts`) with a few verbs, and a component draws it (`Bot.tsx`):
 *
 * | Verb      | When the scene calls it                                   |
 * |-----------|-----------------------------------------------------------|
 * | `setPose` | the Bot's state changed, or it idles (rest, work, nap...) |
 * | `hop`     | the Bot needs the person: once per call, so it is found   |
 * | `listen`  | a finger rests on it: turn to that point and show it      |
 * | `pointAt` | it has something to show: aim a hand at that point        |
 * | `carry`   | it holds a thing to hand over, or lets go of it           |
 * | `sleep`   | it naps: eyes closed, still                               |
 * | `walkTo`  | it goes somewhere on its plot                             |
 * | `speak`   | it says its line                                          |
 */
export type BotPose = 'rest' | 'work' | 'think' | 'stand' | 'ask' | 'slump' | 'listen' | 'point' | 'sleep'
export type BotState = 'idle' | 'working' | 'needs' | 'failed'
