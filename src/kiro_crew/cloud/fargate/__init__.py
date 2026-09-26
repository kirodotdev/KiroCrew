"""Fargate task definitions and ``RunTask`` requests, produced as data.

No module here calls AWS. The launcher's one ``aws`` CLI chokepoint lives in
:mod:`kiro_crew.cloud.aws` and refuses non-read-only calls from an agent
session; a Fargate launch engine performs the calls, and this package decides
what those calls would say. The two security decisions a crew task carries, the
revision key and how the model credential reaches the container, are therefore
properties of a ``dict`` that a test can state without a credential.

WHAT IS ON THIS SURFACE, AND WHY IT MATTERS THAT THE LIST IS COMPLETE.

A consumer of these payloads must never re-spell a name this package decides. The
failure is not cosmetic: a launch engine that wrote its own ``"kirocrew:task"``
instead of importing :data:`LAUNCH_TAG_KEY` classified every managed task as
foreign, so its teardown planner returned an empty delete set and reported
success while every task kept running and kept billing. Absent from an export
list read as a value, the same way an empty collection and a missing field did.

So a constant belongs here on either of two limbs, mirroring the closed-set rule
in :mod:`kiro_crew.cloud.fargate.runtask`: a caller cannot build an acceptable
input without reading it (the Fargate size table, the storage bounds, the
``startedBy`` cap, the architectures, the refused credential names), or a caller
cannot interpret a produced payload without reading it (the tag keys and values,
the registration floor, the container name and port). A test derives both limbs
from the source and fails when a constant satisfying either one is missing here,
so the seventh addition is covered before a consumer trips over it.

The derivation prefixes and suffixes are deliberately NOT here --
``FAMILY_PREFIX``, ``SECRET_NAME_PREFIX``, ``EXECUTION_ROLE_SUFFIX``,
``TASK_ROLE_SUFFIX``. The derived NAME is already exposed as a function
(:func:`task_family`, :func:`execution_role_arn`, :func:`task_role_arn`), and
exporting the fragment it is built from would offer a second way to spell what
the function returns -- which is the liability this module has closed four times
already. Call the function.
"""

from kiro_crew.cloud.fargate.identity import (
    CrewBinding,
    DocumentRefused,
    SecretRef,
    agree,
    bindings_in_document,
    execution_role_arn,
    log_group_name,
    parse_role_arn,
    parse_secret_arn,
    secret_env_name,
    sole_binding,
    task_family,
    task_role_arn,
    validated_crew_name,
    validated_region,
)
from kiro_crew.cloud.fargate.runtask import (
    CLOSED_ENV,
    CONTAINER_OVERRIDE_KEYS,
    DERIVED_ENV,
    EPHEMERAL_STORAGE_MAX_GIB,
    EPHEMERAL_STORAGE_MIN_GIB,
    FARGATE_MEMORY_FOR_CPU,
    LAUNCH_TAG_KEY,
    REFUSED_ENV,
    STARTED_BY_MAX,
    TASK_OVERRIDE_KEYS,
    TASK_TTL_ENV,
    Placement,
    TaskSize,
    derived_environment,
    run_task_request,
)
from kiro_crew.cloud.fargate.taskdef import (
    CPU_ARCHITECTURES,
    CREDENTIAL_ENV,
    CREW_CONTAINER_NAME,
    CREW_STOP_TIMEOUT_SECS,
    CREW_TAG_KEY,
    FINGERPRINT_TAG_KEY,
    FRONT_PORT,
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
    MODEL_CREDENTIAL_ENV,
    OPERATING_SYSTEM_FAMILY,
    REGISTRATION_CPU,
    REGISTRATION_MEMORY,
    LogSpec,
    StoreSpec,
    TaskDefinitionSpec,
    credential_recipient,
    default_log_spec,
    revision_fingerprint,
    secret_destinations,
    spec_binding,
    task_definition_document,
)

__all__ = [
    "CLOSED_ENV",
    "CONTAINER_OVERRIDE_KEYS",
    "CPU_ARCHITECTURES",
    "CREDENTIAL_ENV",
    "DERIVED_ENV",
    "EPHEMERAL_STORAGE_MAX_GIB",
    "EPHEMERAL_STORAGE_MIN_GIB",
    "FARGATE_MEMORY_FOR_CPU",
    "OPERATING_SYSTEM_FAMILY",
    "REFUSED_ENV",
    "REGISTRATION_CPU",
    "REGISTRATION_MEMORY",
    "STARTED_BY_MAX",
    "CREW_CONTAINER_NAME",
    "CREW_STOP_TIMEOUT_SECS",
    "CREW_TAG_KEY",
    "CrewBinding",
    "SecretRef",
    "DocumentRefused",
    "FINGERPRINT_TAG_KEY",
    "FRONT_PORT",
    "LAUNCH_TAG_KEY",
    "LogSpec",
    "MANAGED_TAG_KEY",
    "MANAGED_TAG_VALUE",
    "MODEL_CREDENTIAL_ENV",
    "Placement",
    "StoreSpec",
    "TASK_OVERRIDE_KEYS",
    "TASK_TTL_ENV",
    "TaskDefinitionSpec",
    "TaskSize",
    "agree",
    "bindings_in_document",
    "default_log_spec",
    "derived_environment",
    "execution_role_arn",
    "log_group_name",
    "parse_role_arn",
    "parse_secret_arn",
    "revision_fingerprint",
    "run_task_request",
    "secret_destinations",
    "secret_env_name",
    "sole_binding",
    "credential_recipient",
    "spec_binding",
    "task_definition_document",
    "task_family",
    "task_role_arn",
    "validated_crew_name",
    "validated_region",
]
