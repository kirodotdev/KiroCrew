# An SSM crew's target can name an Auto Scaling group or an SSM parameter, looked up each time its tunnel is built

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision
On the `ssm` connection method, `ssm_target` may be `asg:<group>` (the group's one InService instance) or `param:<name>` (an SSM parameter holding the instance id) besides an `i-`/`mi-` id. The hub looks it up with the crew's own AWS profile and region each time it builds the tunnel and never writes the answer back; the id it gets is validated like a typed one; no or several InService instances fail closed with their own Diagnose reason; such a crew is never treated as a cloud-launched crew; plain ids work as before. Tag filters are not part of it.

## Why
- Looking it up when the tunnel is built is what lets a replaced instance be picked up on the next rebuild, and the change only adds an opt-in shape.
- A prefix on `ssm_target` keeps the registry schema as it is, leaves a plain id literal, and needs no precedence rule against a second field.
- During a rolling replacement a group can briefly have two InService instances, so more than one fails closed and the next attempt picks it up; a tag filter is ambiguous all the time and needs its own selection rule.

## Evidence
- The decision, on the issue: https://github.com/kirodotdev/KiroCrew/issues/20247#issuecomment-6715664246
- The triage that put the three open questions, and the answers the decision adopts: https://github.com/kirodotdev/KiroCrew/issues/20247#issuecomment-6282404918 and https://github.com/kirodotdev/KiroCrew/issues/20247#issuecomment-6284015594
- The Diagnose step that came first: https://github.com/kirodotdev/KiroCrew/pull/33181
