# Inline Action Buttons

## Scope

`action::` is an inline-action value protocol inside legacy Slack OPTIONS controls. It is not a general Block Kit routing protocol: `kiro_crew.slack.interactions.dispatch` calls `_handle_options` only for action IDs with `OPTIONS_ACTION_PREFIX`, which `kiro_crew.slack.format` defines for OPTIONS choices. Other action IDs reach the tool-approval fallback when the interaction supplies a channel and message. `test_unknown_action_id_falls_through_to_tool_approval` locks that fallback.

The dispatcher first requires `is_allowed_user(user_id)`. OPTIONS interactions also pass `channel_inbound_permitted("slack")` before their handler runs. These gates are load-bearing because the action value becomes agent-visible context and a routed turn.

## Button routing

An OPTIONS choice whose `value` starts with `action::` enters the action branch of `kiro_crew.slack.interactions._handle_options`. The remainder of `value` is an opaque payload; the handler does not parse or require JSON. It derives the visible label from `action["text"]["text"]`, falling back to the selected overflow option's text.

`_route_action_to_session` performs the shared delivery:

1. It redacts exfiltration URLs and credentials from the label, then attempts to replace matching elements in the source message with a context label.
2. It posts the redacted label as a visible reply in the source thread. A failed post aborts routing, so an agent turn never runs without its visible Slack message. `test_post_message_failure_aborts` locks this ordering.
3. It redacts and bounds the payload according to `_ACTION_PAYLOAD_CAP`, records the Slack access event, and builds an `Action button clicked` context entry.
4. It calls `kiro_crew.slack.handler.handle_message` with the source message's `thread_ts`, the new reply timestamp, the visible label, and `action_context`.

`ContextBuilder.build_message` appends a non-empty `action_context` before the actual message text. The payload is therefore delivered as context rather than displayed verbatim in the thread; `test_redaction_applied_to_payload` covers payload redaction.

The source-message update is best-effort. `_route_action_to_session` logs and continues when `update_message` fails, so a successful route does not guarantee that the original button has been replaced visually.

## Clicked-message rendering

`_mark_button_clicked` walks every `actions` block. For each block containing the supplied action ID, it removes every matching element, inserts a `context` block containing `✓ {label}` immediately before that actions block, and omits the actions block when no elements remain. It preserves blocks without a matching element. `TestMarkButtonClicked` covers replacement, unchanged input when no match exists, and removal of an empty actions block.

The identifier match is the load-bearing link between Slack's interaction payload and the rendered message. Reused action IDs in separate actions blocks produce a context label for each matching block.

## Extended elements

`_handle_options` contains a direct-handler branch for an `action_id` beginning with `action::`. It parses the suffix as a JSON object, obtains a selection through `_extract_selected_value`, adds `selected_value`, derives a label from `placeholder.text` and the selected display text, and routes it through `_route_action_to_session`. `_extract_selected_value` handles `selected_option`, date, time, and datetime fields; malformed JSON or a non-object payload stops this branch without routing.

This branch is not reachable through the normal Slack dispatcher: `dispatch` forwards only `OPTIONS_ACTION_PREFIX` action IDs to `_handle_options`, while an `action::` action ID falls through to `_handle_tool_approval`. `test_extended_element_happy_path`, `test_malformed_json_in_action_id_no_crash`, and `test_non_dict_json_in_action_id_no_crash` exercise `_handle_options` directly, not the dispatcher.

An element with an `OPTIONS_ACTION_PREFIX` action ID can enter the existing value branch when its selected value starts with `action::`, but that selected value is the opaque payload. It does not activate the direct-handler branch or merge a base JSON object with `selected_value`. Agents must not rely on `action::` in an extended element's `action_id` as an available Slack protocol.

## Tests

`test/test_action_interactions.py` covers the direct action-handler path, payload redaction, audit logging, and the block-transforming helpers. `test/test_slack_interactions_coverage.py::TestDispatchPayloadParsing::test_unknown_action_id_falls_through_to_tool_approval` covers the dispatch boundary that excludes arbitrary action IDs.
