/**
 * Silent context sent to the SDPM agent before the user's first message.
 *
 * This module contains model-facing protocol text only. These exact English
 * lines are SDPM's mode-selection contract and must not be translated.
 */

export const MODE_CONTEXT = {
  spec: 'Interaction mode: dialogue',
  vibe: 'Interaction mode: fast',
  style: 'The user wants to create a reusable style. Call start_style() first.',
} as const
