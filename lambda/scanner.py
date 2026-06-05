"""
scanner.py — CloudFormation Stack Scanner
=========================================
Discovers CloudFormation stacks that have opted into drift monitoring
via a tag, then retrieves the resources inside each tagged stack.

Design decisions:
-----------------
1. We filter at the TAG level (stack-level), not resource level.
   This means teams opt their whole stack in, not individual resources.
   Simpler mental model, and matches how CloudFormation is managed.

2. list_stacks doesn't return tags — we call describe_stacks per stack
   to get them. This is an extra API call per stack but unavoidable
   with the CloudFormation API design. For large accounts (1000+ stacks)
   this is noticeable; a naming convention could reduce calls, but tags
   are more flexible and explicit.

3. We attach the resource list directly to the stack dict so downstream
   check functions don't need a separate API call. One scan, one dict.
"""

import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# CloudFormation stack states that are "live" and can be updated.
# We skip stacks in terminal failure or deleted states — they can't
# be fixed by a stack update anyway.
ACTIVE_STACK_STATUSES = [
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
]


def get_tagged_stacks(tag_key: str, tag_value: str) -> list:
    """
    Returns all active CloudFormation stacks that have the given tag.

    Parameters:
        tag_key   — str: the tag key to filter on (e.g. "drift-monitor")
        tag_value — str: the tag value to match (e.g. "enabled")

    Returns:
        list of stack dicts, each with a "Resources" key containing
        the list of resources inside that stack.

    Raises:
        botocore.exceptions.ClientError if the initial list_stacks call fails.
        Individual stack describe failures are logged and skipped, not raised.
    """
    cf_client = boto3.client("cloudformation")
    matching_stacks = []

    try:
        # -----------------------------------------------------------------
        # Paginate through all active stacks.
        # AWS returns up to 100 stacks per page — the paginator handles
        # calling the API repeatedly until all pages are retrieved.
        # -----------------------------------------------------------------
        paginator = cf_client.get_paginator("list_stacks")
        pages = paginator.paginate(StackStatusFilter=ACTIVE_STACK_STATUSES)

        for page in pages:
            for stack_summary in page.get("StackSummaries", []):
                stack_name = stack_summary["StackName"]

                # -----------------------------------------------------------------
                # describe_stacks gives us the full stack detail including Tags.
                # list_stacks (above) only gives us summaries without tags.
                # -----------------------------------------------------------------
                try:
                    detail_response = cf_client.describe_stacks(StackName=stack_name)
                    stack = detail_response["Stacks"][0]

                    if _has_tag(stack, tag_key, tag_value):
                        logger.info(f"  Tagged for monitoring: '{stack_name}'")

                        # Attach resources now so check functions have everything
                        # they need in one dict, without extra API calls
                        stack["Resources"] = _get_stack_resources(
                            cf_client, stack_name
                        )
                        matching_stacks.append(stack)
                    # Stacks without the tag are silently skipped (expected behaviour)

                except ClientError as e:
                    # Don't abort the whole scan if one stack can't be described.
                    # Log and move on.
                    logger.warning(
                        f"  Could not describe stack '{stack_name}': {e}. Skipping."
                    )
                    continue

    except ClientError as e:
        # list_stacks failing is a fundamental error — raise so handler.py
        # can catch it and report it properly
        logger.error(f"Failed to list CloudFormation stacks: {e}")
        raise

    logger.info(
        f"Scan complete: {len(matching_stacks)} tagged stack(s) found."
    )
    return matching_stacks


def _get_stack_resources(cf_client, stack_name: str) -> list:
    """
    Returns all resources in the given CloudFormation stack.

    Parameters:
        cf_client  — boto3 CloudFormation client (passed in to reuse the connection)
        stack_name — str: the CloudFormation stack name

    Returns:
        list of resource summary dicts, each containing at minimum:
          ResourceType      — e.g. "AWS::Lambda::Function"
          PhysicalResourceId — the actual AWS resource name/ARN
          LogicalResourceId  — the name used in the CloudFormation template
    """
    resources = []

    try:
        paginator = cf_client.get_paginator("list_stack_resources")

        for page in paginator.paginate(StackName=stack_name):
            resources.extend(page.get("StackResourceSummaries", []))

        logger.info(
            f"  Retrieved {len(resources)} resource(s) from stack '{stack_name}'."
        )

    except ClientError as e:
        # Resource listing failing doesn't mean we skip the whole stack —
        # just means we return empty resources and the checks will find nothing.
        logger.warning(
            f"  Could not list resources for stack '{stack_name}': {e}"
        )

    return resources


def _has_tag(stack: dict, tag_key: str, tag_value: str) -> bool:
    """
    Returns True if the stack has a tag matching both key and value.

    CloudFormation returns tags as a list of {"Key": "...", "Value": "..."} dicts,
    not as a simple key-value dict. This helper normalises that.

    Parameters:
        stack     — dict: full CloudFormation stack description
        tag_key   — str: the key to look for
        tag_value — str: the value that must match

    Returns:
        bool
    """
    for tag in stack.get("Tags", []):
        if tag.get("Key") == tag_key and tag.get("Value") == tag_value:
            return True
    return False
