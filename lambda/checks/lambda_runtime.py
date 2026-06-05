"""
checks/lambda_runtime.py — Lambda Runtime EOL Check
====================================================
Inspects every Lambda function in a CloudFormation stack and flags
runtimes that are deprecated, EOL, or approaching EOL.

Why call the Lambda API rather than reading the CF template?
------------------------------------------------------------
CloudFormation templates can lag behind the actual deployed state.
If someone updated a function's runtime outside of CloudFormation,
the template still shows the old value. We want to check what's
ACTUALLY running, not what's in the template.

So: for each AWS::Lambda::Function resource we find in the stack,
we call lambda:GetFunctionConfiguration to get the live runtime.

Severity levels:
----------------
  CRITICAL — runtime is EOL or blocked. Functions cannot be updated
             (EOL) or cannot be invoked (blocked). Immediate action needed.
  WARNING  — runtime is deprecated, OR it's active but within the
             warning window (default 90 days) of its EOL date.

Finding format (dict):
----------------------
Every finding is a dict with consistent keys so notifier.py can
format them uniformly regardless of which check produced them.
Required keys: severity, stack_name, function_name, logical_id,
               runtime, issue, detail, action, eol_info
"""

import logging
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# The CloudFormation resource type string for Lambda functions.
# We filter stack resources by this type.
LAMBDA_RESOURCE_TYPE = "AWS::Lambda::Function"


def check_lambda_runtimes(stack: dict, eol_data: dict, config: dict) -> list:
    """
    Checks all Lambda functions in a stack against EOL data.

    Parameters:
        stack    — dict: CloudFormation stack dict with "Resources" attached
        eol_data — dict: structured EOL data from ai_parser / eol_cache
        config   — dict: runtime config (needs "days_eol_warning")

    Returns:
        list of Finding dicts (empty list if no issues found)
    """
    stack_name = stack["StackName"]
    findings = []

    # -----------------------------------------------------------------
    # Build a fast lookup dict from the runtime list.
    # e.g. {"python3.8": {"status": "deprecated", ...}, ...}
    # Without this we'd do a linear search for every function.
    # -----------------------------------------------------------------
    runtime_eol_map = {
        r["runtime_id"]: r
        for r in eol_data.get("runtimes", [])
    }

    if not runtime_eol_map:
        logger.warning(
            "EOL data contains no runtime entries. Cannot run runtime check."
        )
        return findings

    # Filter the stack's resources to Lambda functions only
    lambda_resources = [
        r for r in stack.get("Resources", [])
        if r.get("ResourceType") == LAMBDA_RESOURCE_TYPE
    ]

    if not lambda_resources:
        logger.info(f"  No Lambda functions found in stack '{stack_name}'.")
        return findings

    logger.info(
        f"  {len(lambda_resources)} Lambda function(s) to check in '{stack_name}'."
    )

    lambda_client = boto3.client("lambda")
    warning_days = config["days_eol_warning"]

    for resource in lambda_resources:
        function_name = resource.get("PhysicalResourceId")
        logical_id = resource.get("LogicalResourceId", "unknown")

        # PhysicalResourceId is null if the resource hasn't been created yet
        # (e.g. stack is in a partial creation state)
        if not function_name:
            logger.warning(
                f"  Lambda resource '{logical_id}' has no PhysicalResourceId. "
                "Stack may be in a partial state. Skipping."
            )
            continue

        # -----------------------------------------------------------------
        # Get the ACTUAL deployed runtime from the Lambda API.
        # This is the authoritative value — not the CF template.
        # -----------------------------------------------------------------
        runtime = _get_function_runtime(lambda_client, function_name)

        if runtime is None:
            # Error already logged by _get_function_runtime
            continue

        if not runtime or runtime.startswith("provided"):
            # Custom/provided runtimes (e.g. Rust, C++) are not in the
            # standard EOL table — users manage their own runtime lifecycle
            logger.info(
                f"  '{function_name}' uses runtime '{runtime}' (custom). Skipping."
            )
            continue

        logger.info(f"  Checking: {function_name} (runtime: {runtime})")

        # Look up this runtime in EOL data
        eol_info = runtime_eol_map.get(runtime)

        if not eol_info:
            # Not in our EOL data. Could be a very new runtime, or incomplete data.
            logger.info(
                f"  Runtime '{runtime}' not found in EOL data. "
                "May be new or not yet tracked."
            )
            continue

        # Evaluate and produce a finding if there's an issue
        finding = _evaluate_runtime(
            stack_name=stack_name,
            function_name=function_name,
            logical_id=logical_id,
            runtime=runtime,
            eol_info=eol_info,
            warning_days=warning_days,
        )

        if finding:
            findings.append(finding)

    return findings


def _get_function_runtime(lambda_client, function_name: str) -> str | None:
    """
    Fetches the current runtime of a Lambda function.

    Parameters:
        lambda_client — boto3 Lambda client
        function_name — str: function name or ARN

    Returns:
        str: runtime identifier (e.g. "python3.12"), or None on error
    """
    try:
        config = lambda_client.get_function_configuration(
            FunctionName=function_name
        )
        return config.get("Runtime", "")

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "ResourceNotFoundException":
            # Function exists in CF but not in Lambda — stack drift
            logger.warning(
                f"  Function '{function_name}' not found in Lambda API. "
                "Stack may be drifted — resource exists in CF but not deployed."
            )
        else:
            logger.warning(
                f"  Could not get config for function '{function_name}': {e}"
            )

        return None


def _evaluate_runtime(
    stack_name: str,
    function_name: str,
    logical_id: str,
    runtime: str,
    eol_info: dict,
    warning_days: int,
) -> dict | None:
    """
    Evaluates a runtime against its EOL info and returns a Finding or None.

    Checks in priority order (most severe first):
      1. Runtime is EOL or blocked → CRITICAL
      2. Runtime is deprecated     → WARNING
      3. Runtime EOL within warning window → WARNING
      4. Runtime is fine           → None

    Parameters:
        stack_name    — str: for the finding context
        function_name — str: physical resource ID (actual function name)
        logical_id    — str: CloudFormation logical ID
        runtime       — str: the runtime identifier (e.g. "python3.8")
        eol_info      — dict: the EOL entry for this runtime from the AI-parsed data
        warning_days  — int: days before EOL to start warning

    Returns:
        dict: a Finding, or None if no issue
    """
    status = eol_info.get("status", "unknown")
    eol_date_str = eol_info.get("eol_date")
    deprecation_date_str = eol_info.get("deprecation_date")
    now = datetime.now(timezone.utc)

    # -----------------------------------------------------------------
    # Check 1: Terminal bad state — CRITICAL
    # EOL means functions can't be updated. Blocked means they can't run.
    # -----------------------------------------------------------------
    if status in ("eol", "blocked"):
        return {
            "severity":     "CRITICAL",
            "stack_name":   stack_name,
            "function_name": function_name,
            "logical_id":   logical_id,
            "runtime":      runtime,
            "issue":        f"Runtime '{runtime}' is {status.upper()}",
            "detail": (
                f"This runtime has reached end-of-life. "
                f"Status: {status}. EOL date: {eol_date_str or 'unknown'}."
            ),
            "action": (
                "Upgrade this Lambda runtime as soon as possible. "
                "Test the new runtime in a dev environment before applying here."
            ),
            "eol_info": eol_info,
        }

    # -----------------------------------------------------------------
    # Check 2: Deprecated — WARNING
    # AWS has announced deprecation; action is needed soon.
    # -----------------------------------------------------------------
    if status == "deprecated":
        return {
            "severity":     "WARNING",
            "stack_name":   stack_name,
            "function_name": function_name,
            "logical_id":   logical_id,
            "runtime":      runtime,
            "issue":        f"Runtime '{runtime}' is DEPRECATED",
            "detail": (
                f"Deprecated on: {deprecation_date_str or 'unknown'}. "
                f"EOL date: {eol_date_str or 'unknown'}."
            ),
            "action": (
                "Plan a runtime upgrade. "
                "Test the new runtime in a dev environment before applying to this stack."
            ),
            "eol_info": eol_info,
        }

    # -----------------------------------------------------------------
    # Check 3: Active but EOL is approaching — WARNING
    # Runtime is still supported but the clock is ticking.
    # -----------------------------------------------------------------
    if status == "active" and eol_date_str:
        try:
            eol_date = datetime.fromisoformat(eol_date_str)

            # Ensure timezone awareness
            if eol_date.tzinfo is None:
                eol_date = eol_date.replace(tzinfo=timezone.utc)

            days_remaining = (eol_date - now).days

            if 0 < days_remaining <= warning_days:
                return {
                    "severity":     "WARNING",
                    "stack_name":   stack_name,
                    "function_name": function_name,
                    "logical_id":   logical_id,
                    "runtime":      runtime,
                    "issue": (
                        f"Runtime '{runtime}' reaches EOL in {days_remaining} day(s)"
                    ),
                    "detail": (
                        f"EOL date: {eol_date_str}. "
                        f"{days_remaining} day(s) remaining."
                    ),
                    "action": (
                        f"Plan a runtime upgrade within the next {days_remaining} day(s). "
                        "Test in a dev environment first."
                    ),
                    "eol_info": eol_info,
                }

        except ValueError as e:
            logger.warning(
                f"Could not parse EOL date '{eol_date_str}' for runtime '{runtime}': {e}"
            )

    # No issues found — return None (handler.py skips None findings)
    return None
