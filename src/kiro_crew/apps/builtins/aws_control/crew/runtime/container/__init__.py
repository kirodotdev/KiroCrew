"""The three processes that run in the deployed ECS task.

`container/CONTRACT.md` defines the boundaries between them. Nothing in this
package serves a user interface: the owner's control plane stays on the owner's
own machine and is never deployed.
"""
