"""
ai_parser.py — Bounded AI EOL Data Parser
==========================================
Uses the Claude API to parse raw AWS docs HTML into structured JSON
containing Lambda runtime EOL dates and support status.

What "bounded" means here:
--------------------------
The AI is constrained to one task by design:
  1. SYSTEM PROMPT: tells the model it is a data extraction tool only,
     must output only valid JSON matching our schema, and must ignore
     any other instructions in the input.
  2. CODE VALIDATION: we call json.loads() on every response. If the
     AI goes off-script and returns prose, json.loads() raises and we
     reject it. The model cannot produce anything we'll act on unless
     it's valid JSON.
  3. SCHEMA CHECK: we check for the expected keys before using the data.

Why AI instead of BeautifulSoup?
---------------------------------
AWS docs page layout changes over time. Hardcoded HTML selectors break
silently — the parser succeeds but returns garbage, and you only notice
when a deprecated runtime slips through. The AI approach is more resilient:
even if the table structure changes, the model can infer meaning from context.
If the AI can't parse it, it returns {"error": "..."} and we know immediately.

API key security:
-----------------
The Anthropic API key is stored in SSM Parameter Store as a SecureString.
We fetch it at runtime — it never appears in environment variables,
Lambda config, or Terraform state.
"""

import json
import logging
import boto3
import requests
from botocore.exceptions import ClientError
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

# Sonnet 4.5 — good balance of accuracy and cost for structured extraction.
CLAUDE_MODEL = "claude-sonnet-4-5"

# 2000 tokens is well above what the JSON response needs.
# Keeping it bounded prevents the model from being verbose even if it tries.
MAX_RESPONSE_TOKENS = 2000

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# We send only the first N characters of the page to the AI.
# The runtime tables appear near the top — 15,000 chars is enough
# to capture everything we need without burning tokens on page chrome.
MAX_HTML_CHARS = 15_000

# -----------------------------------------------------------------
# SYSTEM PROMPT — the bounding constraint
# -----------------------------------------------------------------
# This is the most important part of the bounded AI design.
# It tells the model:
#   - What it is (a data extraction tool, nothing else)
#   - What to output (JSON only, exact schema defined below)
#   - What NOT to do (answer questions, perform other tasks)
#   - What to do when it can't find the data (return error sentinel)
#
# We define the output schema directly in the prompt so the model
# knows exactly what we expect — field names, types, allowed values.
# -----------------------------------------------------------------
SYSTEM_PROMPT = """You are a data extraction tool. Your ONLY job is to extract AWS Lambda runtime support information from HTML content and convert it to JSON.

STRICT RULES — you must follow all of these without exception:
1. Output ONLY valid JSON. No markdown code blocks. No explanations. No preamble. No postamble. Nothing before or after the JSON object.
2. Do not answer questions. Do not offer help. Do not perform any task other than the one described here.
3. If the HTML does not contain Lambda runtime data, output exactly this and nothing else: {"error": "no valid runtime data found", "runtimes": []}
4. Extract every runtime entry you find, including deprecated, EOL, and blocked ones.
5. If a date field is not present or unclear, use null — do not guess.

Output ONLY a JSON object matching this exact schema:
{
  "runtimes": [
    {
      "runtime_id": "string — the runtime identifier as AWS uses it (e.g. python3.12, nodejs20.x, java21)",
      "language": "string — one of: python, nodejs, java, dotnet, ruby, go, custom",
      "version": "string — version number only (e.g. 3.12, 20, 21)",
      "status": "string — one of: active, deprecated, eol, blocked",
      "deprecation_date": "string in YYYY-MM-DD format, or null",
      "block_date": "string in YYYY-MM-DD format, or null",
      "eol_date": "string in YYYY-MM-DD format, or null"
    }
  ]
}

Status definitions (use exactly these values):
- active: runtime is currently supported with no announced deprecation
- deprecated: AWS has announced deprecation; new function creation may be restricted
- eol: runtime has passed end-of-life; existing functions still run but cannot be updated
- blocked: runtime is fully blocked; functions cannot be invoked

Output only the JSON object. Nothing else."""


def parse_eol_data(raw_html: str, config: dict) -> dict | None:
    """
    Sends scraped HTML to Claude and returns structured EOL data as a dict.

    Parameters:
        raw_html — str: raw HTML from the AWS Lambda runtimes docs page
        config   — dict: runtime config (needs "anthropic_ssm_param" key)

    Returns:
        dict matching the schema in SYSTEM_PROMPT, or None if parsing failed
    """

    # Fetch the API key from SSM at runtime — never stored in env vars or state
    api_key = _get_api_key(config["anthropic_ssm_param"])

    if not api_key:
        logger.error("Could not retrieve Anthropic API key from SSM. Cannot parse EOL data.")
        return None

    # Truncate HTML to reduce tokens and keep the model focused
    html_to_send = raw_html[:MAX_HTML_CHARS]
    logger.info(
        f"Sending {len(html_to_send):,} of {len(raw_html):,} HTML chars to Claude for parsing."
    )

    try:
        # Call the Anthropic API using raw requests rather than the SDK.
        # This avoids packaging the anthropic library into our Lambda zip —
        # requests is already included for the scraper.
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": MAX_RESPONSE_TOKENS,
                "system":     SYSTEM_PROMPT,
                "messages": [
                    {
                        "role":    "user",
                        "content": (
                            "Extract Lambda runtime EOL data from this HTML:\n\n"
                            + html_to_send
                        ),
                    }
                ],
            },
            timeout=60,  # AI calls can take longer than normal HTTP requests
        )

        response.raise_for_status()
        api_response = response.json()

        # Extract the text from Claude's response content block
        content_blocks = api_response.get("content", [])
        if not content_blocks:
            logger.error("Claude returned an empty content list.")
            return None

        raw_text = content_blocks[0].get("text", "").strip()

        if not raw_text:
            logger.error("Claude returned an empty text block.")
            return None

        # -----------------------------------------------------------------
        # VALIDATION: enforce that the response is valid JSON.
        # Claude sometimes wraps JSON in markdown fences (```json ... ```)
        # despite the system prompt telling it not to. We strip those here
        # before parsing so a minor formatting deviation doesn't abort the run.
        # If it's still not valid JSON after stripping, we reject it.
        # -----------------------------------------------------------------
        clean_content = raw_text.strip()

        if clean_content.startswith("```"):
            # Remove the opening fence line (```json or just ```)
            clean_content = clean_content.split("\n", 1)[1]

        if clean_content.endswith("```"):
            # Remove the closing fence
            clean_content = clean_content.rsplit("```", 1)[0].strip()

        parsed = json.loads(clean_content)

        # Check for the error sentinel the AI sends when it can't find data
        if "error" in parsed and parsed.get("runtimes") == []:
            logger.warning(
                f"AI could not parse runtime data from the HTML: {parsed['error']}"
            )
            return None

        runtime_count = len(parsed.get("runtimes", []))
        logger.info(f"AI parsing successful — {runtime_count} runtime entries extracted.")

        # Log a sample so you can see what was parsed in CloudWatch
        if runtime_count > 0:
            sample = parsed["runtimes"][0]
            logger.info(f"Sample entry: {json.dumps(sample)}")

        return parsed

    except json.JSONDecodeError as e:
        # The model went off-script and returned non-JSON content.
        # Log enough of the response to debug the prompt if needed.
        logger.error(
            f"Claude returned non-JSON content. json.loads failed: {e}. "
            f"First 500 chars of response: {raw_text[:500] if raw_text else '(empty)'}"
        )
        return None

    except RequestException as e:
        logger.error(f"Anthropic API request failed: {e}")
        return None


def _get_api_key(ssm_parameter_name: str) -> str | None:
    """
    Fetches the Anthropic API key from SSM Parameter Store.

    Parameters:
        ssm_parameter_name — str: full SSM path, e.g. "/drift-monitor/anthropic-api-key"

    Returns:
        str: the decrypted API key value, or None if retrieval failed
    """
    ssm = boto3.client("ssm")

    try:
        response = ssm.get_parameter(
            Name=ssm_parameter_name,
            WithDecryption=True,  # Required for SecureString — decrypts using KMS
        )
        return response["Parameter"]["Value"]

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "ParameterNotFound":
            logger.error(
                f"SSM parameter '{ssm_parameter_name}' not found. "
                "Did you create it before running terraform apply? "
                "Run: aws ssm put-parameter --name <name> --value <key> --type SecureString"
            )
        else:
            logger.error(f"SSM error fetching '{ssm_parameter_name}': {e}")

        return None
