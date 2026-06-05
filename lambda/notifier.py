"""
notifier.py — Alert Notifier
=============================
Sends drift monitoring findings to SNS (email) and CloudWatch logs.

Design decisions:
-----------------
1. CloudWatch always gets the full report, every run.
   It's free (within limits), always on, and searchable.

2. SNS only fires when there are findings or errors.
   We don't want clean-run emails cluttering inboxes.
   Teams can check CloudWatch to confirm clean runs happened.

3. The report format is plain text, not HTML.
   Plain text renders correctly in all email clients.
   SNS email subscriptions receive the raw Message string.

Phase 2 will add Slack/Teams webhook support here.
The check logic doesn't need to change — only this file.
"""

import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def send_report(
    config: dict,
    findings: list,
    error: str = None,
    stack_count: int = 0,
) -> None:
    """
    Sends the drift monitoring report to CloudWatch and optionally SNS.

    Parameters:
        config      — dict: runtime config (needs "sns_topic_arn")
        findings    — list: list of Finding dicts from check functions
        error       — str: optional error message if the run had a problem
        stack_count — int: total stacks checked this run
    """
    report = _build_report(findings, error, stack_count)

    # CloudWatch: always log, every run
    _log_report(report, findings)

    # SNS: only when there's something to alert on
    if findings or error:
        _send_sns(config["sns_topic_arn"], report, findings)
    else:
        logger.info(
            "Clean run — no findings, no errors. "
            "Skipping SNS (check CloudWatch for run confirmation)."
        )


def _build_report(findings: list, error: str | None, stack_count: int) -> dict:
    """Builds a structured summary dict from the findings."""
    critical = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    warnings = sum(1 for f in findings if f.get("severity") == "WARNING")

    return {
        "run_timestamp":  datetime.now(timezone.utc).isoformat(),
        "stacks_checked": stack_count,
        "total_findings": len(findings),
        "critical_count": critical,
        "warning_count":  warnings,
        "error":          error,
        "findings":       findings,
    }


def _log_report(report: dict, findings: list) -> None:
    """
    Logs the full report to CloudWatch via Python logging.
    Lambda forwards all logger output to the function's log group.
    """
    logger.info("=== DRIFT MONITOR REPORT ===")
    logger.info(f"Run time      : {report['run_timestamp']}")
    logger.info(f"Stacks checked: {report['stacks_checked']}")
    logger.info(
        f"Findings      : {report['total_findings']} "
        f"(CRITICAL: {report['critical_count']}, "
        f"WARNING: {report['warning_count']})"
    )

    if report.get("error"):
        logger.error(f"Run error: {report['error']}")

    for i, finding in enumerate(findings, 1):
        # Log at WARNING level so findings stand out in CloudWatch
        # and can be filtered/alarmed on separately
        logger.warning(
            f"[{finding['severity']}] {i}/{len(findings)} — "
            f"Stack: {finding['stack_name']} | "
            f"Function: {finding['function_name']} | "
            f"Issue: {finding['issue']}"
        )
        logger.warning(f"  Detail: {finding['detail']}")
        logger.warning(f"  Action: {finding['action']}")

    logger.info("=== END REPORT ===")


def _send_sns(topic_arn: str, report: dict, findings: list) -> None:
    """
    Publishes the report to SNS as a human-readable plain text email.

    Parameters:
        topic_arn — str: the SNS topic ARN to publish to
        report    — dict: the structured report
        findings  — list: the findings list (for formatting)
    """
    sns = boto3.client("sns")

    subject = _build_subject(report)
    body = _build_email_body(report, findings)

    try:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],  # SNS subject max is 100 chars
            Message=body,
        )
        logger.info(f"SNS notification sent. Subject: '{subject}'")

    except ClientError as e:
        # Don't raise — a failed SNS publish shouldn't abort the whole run.
        # The findings are still in CloudWatch.
        logger.error(
            f"Failed to send SNS notification: {e}. "
            "Findings are still logged to CloudWatch."
        )


def _build_subject(report: dict) -> str:
    """Returns an appropriate email subject based on severity."""
    if report.get("error"):
        return "[DRIFT MONITOR] ERROR — check CloudWatch logs"
    elif report["critical_count"] > 0:
        return (
            f"[DRIFT MONITOR] CRITICAL — "
            f"{report['critical_count']} critical finding(s)"
        )
    elif report["warning_count"] > 0:
        return (
            f"[DRIFT MONITOR] WARNING — "
            f"{report['warning_count']} warning(s)"
        )
    else:
        return "[DRIFT MONITOR] Report"


def _build_email_body(report: dict, findings: list) -> str:
    """
    Formats the report as plain text for email delivery.
    Plain text is used because SNS email subscriptions receive
    the raw Message string — no HTML rendering.
    """
    sep = "=" * 60
    thin = "-" * 40

    lines = [
        sep,
        "DRIFT MONITOR REPORT",
        sep,
        f"Run time      : {report['run_timestamp']}",
        f"Stacks checked: {report['stacks_checked']}",
        f"Total findings: {report['total_findings']}",
        f"  CRITICAL    : {report['critical_count']}",
        f"  WARNING     : {report['warning_count']}",
        "",
    ]

    if report.get("error"):
        lines += [
            "ERROR",
            thin,
            report["error"],
            "",
        ]

    if not findings:
        lines.append("No issues found in this run.")
    else:
        lines += ["FINDINGS", thin]

        for i, f in enumerate(findings, 1):
            lines += [
                "",
                f"[{f['severity']}] Finding {i} of {len(findings)}",
                f"Stack    : {f['stack_name']}",
                f"Function : {f['function_name']}",
                f"CF ID    : {f['logical_id']}",
                f"Runtime  : {f['runtime']}",
                f"Issue    : {f['issue']}",
                f"Detail   : {f['detail']}",
                f"Action   : {f['action']}",
            ]

    lines += [
        "",
        sep,
        "Sent by: drift-monitor",
        "Full run logs: CloudWatch → /aws/lambda/drift-monitor",
        sep,
    ]

    return "\n".join(lines)
