"""
tests/test_lambda_runtime.py
============================
Unit tests for the Lambda runtime check.

These tests cover the check logic only — no real AWS calls are made.
All boto3 interactions are mocked using unittest.mock.

Run locally with:
  pip install pytest
  pytest tests/ -v

Design principle:
  Each test covers one clear scenario, named to describe it.
  If a test fails, the name tells you exactly what broke.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

# Add the lambda directory to the Python path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from checks.lambda_runtime import check_lambda_runtimes, _evaluate_runtime


# ======================================================================
# Fixtures — reusable test data builders
# ======================================================================

def make_stack(resources=None, name="test-stack"):
    """Creates a minimal mock CloudFormation stack dict."""
    return {
        "StackName": name,
        "Resources": resources or [],
    }


def make_lambda_resource(
    physical_id="my-function",
    logical_id="MyFunction"
):
    """Creates a mock CloudFormation Lambda function resource summary."""
    return {
        "ResourceType":     "AWS::Lambda::Function",
        "PhysicalResourceId": physical_id,
        "LogicalResourceId": logical_id,
    }


def make_eol_data(runtimes):
    """Creates a mock EOL data dict with a valid scraped_at timestamp."""
    return {
        "runtimes":   runtimes,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def make_eol_entry(
    runtime_id,
    status="active",
    eol_date=None,
    deprecation_date=None,
):
    """Creates a single runtime EOL entry dict."""
    return {
        "runtime_id":        runtime_id,
        "language":          "python",
        "version":           runtime_id.replace("python", ""),
        "status":            status,
        "eol_date":          eol_date,
        "deprecation_date":  deprecation_date,
        "block_date":        None,
    }


def make_config(warning_days=90):
    """Creates a minimal config dict for testing."""
    return {"days_eol_warning": warning_days}


def future_date(days):
    """Returns an ISO date string N days in the future."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def past_date(days):
    """Returns an ISO date string N days in the past."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# ======================================================================
# Tests: _evaluate_runtime
# Tests the finding logic in isolation from boto3 and the stack scanner.
# ======================================================================

class TestEvaluateRuntime:
    """Tests for the core runtime evaluation logic."""

    def _call(self, runtime_id="python3.8", eol_info=None, warning_days=90):
        """Helper to call _evaluate_runtime with minimal boilerplate."""
        return _evaluate_runtime(
            stack_name="test-stack",
            function_name="test-function",
            logical_id="TestFunction",
            runtime=runtime_id,
            eol_info=eol_info or make_eol_entry(runtime_id),
            warning_days=warning_days,
        )

    # --- EOL and blocked (CRITICAL) ---

    def test_eol_status_produces_critical_finding(self):
        """A runtime with status 'eol' must produce a CRITICAL finding."""
        eol_info = make_eol_entry("python3.8", status="eol", eol_date="2025-01-01")
        finding = self._call(eol_info=eol_info)

        assert finding is not None
        assert finding["severity"] == "CRITICAL"
        assert "python3.8" in finding["issue"]
        assert "EOL" in finding["issue"]

    def test_blocked_status_produces_critical_finding(self):
        """A runtime with status 'blocked' must produce a CRITICAL finding."""
        eol_info = make_eol_entry("nodejs10.x", status="blocked")
        finding = self._call("nodejs10.x", eol_info=eol_info)

        assert finding is not None
        assert finding["severity"] == "CRITICAL"

    # --- Deprecated (WARNING) ---

    def test_deprecated_status_produces_warning(self):
        """A deprecated runtime must produce a WARNING finding."""
        eol_info = make_eol_entry(
            "python3.9",
            status="deprecated",
            eol_date=future_date(60),
            deprecation_date=past_date(30),
        )
        finding = self._call("python3.9", eol_info=eol_info)

        assert finding is not None
        assert finding["severity"] == "WARNING"
        assert "DEPRECATED" in finding["issue"]

    # --- Approaching EOL (WARNING) ---

    def test_active_runtime_within_warning_window_produces_warning(self):
        """An active runtime within the EOL warning window should produce a WARNING."""
        eol_info = make_eol_entry(
            "python3.11",
            status="active",
            eol_date=future_date(30),  # 30 days away, inside 90-day window
        )
        finding = self._call("python3.11", eol_info=eol_info, warning_days=90)

        assert finding is not None
        assert finding["severity"] == "WARNING"
        assert "day" in finding["issue"].lower()

    def test_active_runtime_outside_warning_window_produces_no_finding(self):
        """An active runtime far from EOL must not produce any finding."""
        eol_info = make_eol_entry(
            "python3.12",
            status="active",
            eol_date=future_date(365),  # 1 year away, outside 90-day window
        )
        finding = self._call("python3.12", eol_info=eol_info, warning_days=90)

        assert finding is None

    def test_active_runtime_with_no_eol_date_produces_no_finding(self):
        """An active runtime with no known EOL date must not produce a finding."""
        eol_info = make_eol_entry("python3.12", status="active", eol_date=None)
        finding = self._call("python3.12", eol_info=eol_info)

        assert finding is None

    # --- Finding structure validation ---

    def test_critical_finding_has_all_required_keys(self):
        """Every finding must have the full set of expected keys."""
        required_keys = {
            "severity", "stack_name", "function_name", "logical_id",
            "runtime", "issue", "detail", "action", "eol_info"
        }
        eol_info = make_eol_entry("python3.8", status="eol")
        finding = self._call(eol_info=eol_info)

        assert finding is not None
        missing = required_keys - set(finding.keys())
        assert not missing, f"Finding is missing keys: {missing}"

    def test_warning_finding_has_all_required_keys(self):
        """Warning findings must also have all required keys."""
        required_keys = {
            "severity", "stack_name", "function_name", "logical_id",
            "runtime", "issue", "detail", "action", "eol_info"
        }
        eol_info = make_eol_entry("python3.9", status="deprecated")
        finding = self._call("python3.9", eol_info=eol_info)

        assert finding is not None
        missing = required_keys - set(finding.keys())
        assert not missing, f"Finding is missing keys: {missing}"


# ======================================================================
# Tests: check_lambda_runtimes
# Integration-level tests that include the stack resource filtering logic.
# boto3 calls are mocked.
# ======================================================================

class TestCheckLambdaRuntimes:
    """Tests for check_lambda_runtimes including stack resource scanning."""

    def test_stack_with_no_resources_returns_empty(self):
        """A stack with no resources should produce no findings."""
        stack = make_stack(resources=[])
        eol_data = make_eol_data([])

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        assert findings == []

    def test_non_lambda_resources_are_ignored(self):
        """S3 buckets, RDS instances, etc. should not trigger checks."""
        stack = make_stack(resources=[
            {
                "ResourceType":       "AWS::S3::Bucket",
                "PhysicalResourceId": "my-bucket",
                "LogicalResourceId":  "MyBucket",
            },
            {
                "ResourceType":       "AWS::RDS::DBInstance",
                "PhysicalResourceId": "my-db",
                "LogicalResourceId":  "MyDB",
            },
        ])
        eol_data = make_eol_data([])

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        assert findings == []

    def test_empty_eol_data_returns_empty_findings(self):
        """If EOL data has no runtimes, there's nothing to check against."""
        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([])  # No runtime entries

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        assert findings == []

    @patch("checks.lambda_runtime.boto3.client")
    def test_deprecated_runtime_produces_warning_finding(self, mock_boto):
        """A Lambda function with a deprecated runtime must produce a WARNING."""
        # Mock the Lambda API to return a deprecated runtime
        mock_lambda = MagicMock()
        mock_lambda.get_function_configuration.return_value = {
            "Runtime": "python3.8"
        }
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([
            make_eol_entry(
                "python3.8",
                status="deprecated",
                eol_date=future_date(60),
                deprecation_date=past_date(30),
            )
        ])

        findings = check_lambda_runtimes(stack, eol_data, make_config())

        assert len(findings) == 1
        assert findings[0]["severity"] == "WARNING"
        assert findings[0]["runtime"] == "python3.8"
        assert findings[0]["function_name"] == "my-function"
        assert findings[0]["stack_name"] == "test-stack"

    @patch("checks.lambda_runtime.boto3.client")
    def test_eol_runtime_produces_critical_finding(self, mock_boto):
        """A Lambda function with an EOL runtime must produce a CRITICAL finding."""
        mock_lambda = MagicMock()
        mock_lambda.get_function_configuration.return_value = {
            "Runtime": "nodejs10.x"
        }
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([
            make_eol_entry("nodejs10.x", status="eol", eol_date=past_date(180))
        ])

        findings = check_lambda_runtimes(stack, eol_data, make_config())

        assert len(findings) == 1
        assert findings[0]["severity"] == "CRITICAL"

    @patch("checks.lambda_runtime.boto3.client")
    def test_active_healthy_runtime_produces_no_finding(self, mock_boto):
        """A Lambda on a healthy active runtime should produce no finding."""
        mock_lambda = MagicMock()
        mock_lambda.get_function_configuration.return_value = {
            "Runtime": "python3.12"
        }
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([
            make_eol_entry("python3.12", status="active", eol_date=future_date(730))
        ])

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        assert findings == []

    @patch("checks.lambda_runtime.boto3.client")
    def test_runtime_not_in_eol_data_produces_no_finding(self, mock_boto):
        """An unrecognised runtime (not in EOL data) should be skipped."""
        mock_lambda = MagicMock()
        mock_lambda.get_function_configuration.return_value = {
            "Runtime": "python3.99"  # Not in EOL data
        }
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([])  # No entries

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        assert findings == []

    @patch("checks.lambda_runtime.boto3.client")
    def test_provided_custom_runtime_is_skipped(self, mock_boto):
        """Custom/provided runtimes (e.g. Rust) should be skipped."""
        mock_lambda = MagicMock()
        mock_lambda.get_function_configuration.return_value = {
            "Runtime": "provided.al2"
        }
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[make_lambda_resource()])
        eol_data = make_eol_data([
            make_eol_entry("provided.al2", status="active")
        ])

        findings = check_lambda_runtimes(stack, eol_data, make_config())
        # provided runtimes are skipped even if they appear in EOL data
        assert findings == []

    @patch("checks.lambda_runtime.boto3.client")
    def test_multiple_functions_multiple_findings(self, mock_boto):
        """Multiple deprecated functions in one stack should produce multiple findings."""
        mock_lambda = MagicMock()
        # Return different runtimes for different function names
        mock_lambda.get_function_configuration.side_effect = [
            {"Runtime": "python3.8"},   # first call → deprecated
            {"Runtime": "nodejs12.x"},  # second call → also deprecated
        ]
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[
            make_lambda_resource("func-a", "FuncA"),
            make_lambda_resource("func-b", "FuncB"),
        ])
        eol_data = make_eol_data([
            make_eol_entry("python3.8",  status="deprecated"),
            make_eol_entry("nodejs12.x", status="deprecated"),
        ])

        findings = check_lambda_runtimes(stack, eol_data, make_config())

        assert len(findings) == 2
        runtimes_found = {f["runtime"] for f in findings}
        assert runtimes_found == {"python3.8", "nodejs12.x"}

    @patch("checks.lambda_runtime.boto3.client")
    def test_lambda_resource_with_no_physical_id_is_skipped(self, mock_boto):
        """A Lambda resource with no PhysicalResourceId should be skipped gracefully."""
        mock_lambda = MagicMock()
        mock_boto.return_value = mock_lambda

        stack = make_stack(resources=[{
            "ResourceType":       "AWS::Lambda::Function",
            "PhysicalResourceId": None,  # Stack may be partially deployed
            "LogicalResourceId":  "UndeployedFunction",
        }])
        eol_data = make_eol_data([make_eol_entry("python3.8", status="deprecated")])

        findings = check_lambda_runtimes(stack, eol_data, make_config())

        # Should not crash, should produce no findings
        assert findings == []
        # Should not have called the Lambda API
        mock_lambda.get_function_configuration.assert_not_called()
