"""The three processes that run in the deployed ECS task.

`docs/system-specs/modules/aws-control.md` defines the boundaries between them. Nothing in this
package serves a user interface: the owner's control plane stays on the owner's
own machine and is never deployed.
"""
