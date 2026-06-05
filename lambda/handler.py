"""
handler.py — Drift Monitor Lambda Entry Point
=============================================
AWS invokes lambda_handler() when the function runs, whether triggered
by EventBridge (scheduled) or manually via the CLI.

This module is intentionally thin. It coordinates the other modules
in the right order but does no checking or alerting itself.

Flow:
  1. Read config from environment variables (set by Terraform)
  2. Load EOL data — from S3 cache if fresh, or scrape + parse if stale
  3. Find CloudFormation stacks tagged for monitoring
  4. Run checks against each stack
  5. Report all findings via SNS + CloudWatch
"""

import os
import json
import logging
from datetime import datetime, timezone

# Our own modules — each handles one concern
from scanner import get_tagged_stacks
from eol_cache import get_eol_data
from notifier import send_report
from checks.lambda_runtime import check_lambda_runtimes

# -----------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------
# Lambda automatically sends Python log output to CloudWatch Logs.
# We use the root logger here so all child module loggers inherit
# this level. INFO hides DEBUG noise but shows everything meaningful.
# Change to logging.DEBUG temporarily if you need more detail.
# -----------------------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context) -> dict:
    """
    Main Lambda entry point called by AWS.

    Parameters:
        event   — dict sent by the trigger:
                  EventBridge sends a scheduler event dict.
                  Manual invocations should send {"manual": true}.
        context — LambdaContext object (runtime metadata, not used here)

    Returns:
        dict with statusCode (HTTP-style) and a JSON body summary.
        EventBridge ignores the return value, but it's useful for manual runs.
    """

    # -----------------------------------------------------------------
    # Distinguish manual from scheduled runs for logging clarity.
    # Manual runs are identical in behaviour — this is just metadata.
    # -----------------------------------------------------------------
    is_manual = event.get("manual", False)
    run_type = "MANUAL" if is_manual else "SCHEDULED"

    logger.info(
        f"=== Drift Monitor starting [{run_type}] "
        f"at {datetime.now(timezone.utc).isoformat()} ==="
    )

    # -----------------------------------------------------------------
    # Load all config from environment variables.
    # Terraform sets these in the Lambda's environment block.
    # Keeping config here (not scattered across modules) means
    # you can see everything that drives behaviour in one place.
    # -----------------------------------------------------------------
    config = {
        "sns_topic_arn":        os.environ["SNS_TOPIC_ARN"],
        "eol_cache_bucket":     os.environ["EOL_CACHE_BUCKET"],
        "monitor_tag_key":      os.environ["MONITOR_TAG_KEY"],
        "monitor_tag_value":    os.environ["MONITOR_TAG_VALUE"],
        "anthropic_ssm_param":  os.environ["ANTHROPIC_SSM_PARAMETER"],
        "eol_cache_ttl_hours":  int(os.environ.get("EOL_CACHE_TTL_HOURS", "168")),
        "days_eol_warning":     int(os.environ.get("DAYS_UNTIL_EOL_WARNING", "90")),
    }

    logger.info(
        f"Monitoring stacks tagged "
        f"[{config['monitor_tag_key']}={config['monitor_tag_value']}]"
    )

    # -----------------------------------------------------------------
    # Step 1: Load EOL data
    # eol_cache.py decides whether to serve from cache or re-scrape.
    # If this fails we cannot run meaningful checks, so we abort.
    # -----------------------------------------------------------------
    logger.info("Step 1: Loading EOL data...")
    eol_data = get_eol_data(config)

    if not eol_data:
        error_msg = "CRITICAL: Could not load EOL data. Aborting run."
        logger.error(error_msg)
        # Notify even on failure so the team knows the tool broke
        send_report(config, findings=[], error=error_msg)
        return {"statusCode": 500, "body": error_msg}

    runtime_count = len(eol_data.get("runtimes", []))
    logger.info(f"EOL data loaded — {runtime_count} runtime entries available.")

    # -----------------------------------------------------------------
    # Step 2: Discover CloudFormation stacks with our monitoring tag
    # -----------------------------------------------------------------
    logger.info("Step 2: Discovering tagged CloudFormation stacks...")
    stacks = get_tagged_stacks(
        tag_key=config["monitor_tag_key"],
        tag_value=config["monitor_tag_value"],
    )

    logger.info(f"Found {len(stacks)} stack(s) to check.")

    if not stacks:
        logger.warning(
            "No stacks found with the monitor tag. "
            f"Add tag [{config['monitor_tag_key']}={config['monitor_tag_value']}] "
            "to stacks you want monitored."
        )
        send_report(config, findings=[], stack_count=0)
        return {"statusCode": 200, "body": "No tagged stacks found."}

    # -----------------------------------------------------------------
    # Step 3: Run checks against each stack
    # Each check function receives the stack dict + EOL data and returns
    # a list of Finding dicts. We extend all_findings with each result.
    #
    # To add new check types in future phases, add them here following
    # the same pattern as check_lambda_runtimes.
    # -----------------------------------------------------------------
    logger.info("Step 3: Running checks...")
    all_findings = []

    for stack in stacks:
        stack_name = stack["StackName"]
        logger.info(f"  Checking stack: {stack_name}")

        # Phase 1: Lambda runtime check only.
        # Future phases will add: RDS engine, AMI age, etc.
        runtime_findings = check_lambda_runtimes(stack, eol_data, config)

        if runtime_findings:
            logger.info(
                f"    {len(runtime_findings)} issue(s) found in '{stack_name}'"
            )
            all_findings.extend(runtime_findings)
        else:
            logger.info(f"    No issues found in '{stack_name}'")

    # -----------------------------------------------------------------
    # Step 4: Send report
    # notifier.py always logs to CloudWatch.
    # It only sends to SNS when there are findings or errors — we don't
    # want clean-run emails cluttering inboxes.
    # -----------------------------------------------------------------
    logger.info(
        f"Step 4: Sending report — "
        f"{len(all_findings)} finding(s) across {len(stacks)} stack(s)."
    )
    send_report(config, findings=all_findings, stack_count=len(stacks))

    logger.info("=== Drift Monitor complete ===")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "run_type":       run_type,
            "stacks_checked": len(stacks),
            "findings":       len(all_findings),
        }),
    }
