"""The Lambda MicroVM lane's half of the crew runtime image.

The same image serves both lanes. On Fargate ECS starts ``container.supervisor``
directly and the task's environment carries everything it needs. On a MicroVM
nothing can carry an environment to the guest: the platform's only channel is one
HTTP call to one port in the guest, so something has to be listening on that port
before the crew exists, answer the platform's lifecycle hooks, and turn the
payload it is handed into the supervisor's environment.

That is this package. It is a SIBLING of the front process rather than part of
it, for a reason that is not organisational: the front refuses to start without a
control secret, a single-principal declaration and a model identity, and the
platform calls ``/ready`` during the IMAGE BUILD, when none of the three exist
and no crew has been launched. A listener that is the front could not answer the
hook that decides whether the image builds at all.
"""
