"""
eol_scraper.py — AWS Lambda Runtime EOL Scraper
================================================
Fetches the official AWS Lambda runtimes docs page and returns
the raw HTML. Nothing more.

Why so minimal?
---------------
We deliberately keep scraping and parsing separate concerns:
  - eol_scraper.py: gets the HTML (dumb, fast, easy to test)
  - ai_parser.py:   interprets the HTML into structured data

If the AWS docs page layout changes (which it does occasionally),
we only need to update the AI prompt — the scraper stays the same.
If we tried to use BeautifulSoup selectors here, every layout change
would require code changes and redeployment.

Source page: https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
This is the authoritative AWS source for runtime support status.
AWS does not expose this information via any public API.
"""

import logging
import requests
from requests.exceptions import RequestException, Timeout, HTTPError

logger = logging.getLogger(__name__)

# The authoritative AWS source for Lambda runtime support status.
# This URL has been stable for years, but check it if scraping starts failing.
LAMBDA_RUNTIME_DOC_URL = (
    "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html"
)

# Seconds to wait for the HTTP response before giving up.
# AWS docs are fast — 30s is generous. Don't set this too high
# or a slow response can burn Lambda execution time.
REQUEST_TIMEOUT_SECONDS = 30

# A polite User-Agent identifies the tool making the request.
# AWS doesn't require this, but it's good practice and makes
# server-side logs readable if they ever want to reach out.
REQUEST_HEADERS = {
    "User-Agent": (
        "drift-monitor/1.0 "
        "(AWS CloudFormation drift checker; "
        "https://github.com/your-org/drift-monitor)"
    )
}


def scrape_lambda_runtimes() -> str | None:
    """
    Fetches the AWS Lambda runtimes documentation page.

    Returns:
        str: raw HTML of the page, ready to be passed to ai_parser.py
        None: if the request failed for any reason (logged as error)
    """
    logger.info(f"Scraping: {LAMBDA_RUNTIME_DOC_URL}")

    try:
        response = requests.get(
            LAMBDA_RUNTIME_DOC_URL,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        # Raises HTTPError for 4xx and 5xx responses.
        # We catch it below and log clearly rather than letting it
        # bubble up as an unhandled exception.
        response.raise_for_status()

        char_count = len(response.text)
        logger.info(f"Scrape successful — {char_count:,} characters retrieved.")

        # Sanity check: if the page is suspiciously small, it might be
        # a redirect page, error page, or bot-detection response
        if char_count < 5000:
            logger.warning(
                f"Page content is unexpectedly small ({char_count} chars). "
                "This may not be the real docs page. Check the URL."
            )

        return response.text

    except Timeout:
        logger.error(
            f"Request timed out after {REQUEST_TIMEOUT_SECONDS}s. "
            "AWS docs may be slow or unreachable."
        )
        return None

    except HTTPError as e:
        logger.error(
            f"HTTP error fetching docs page: {e.response.status_code} — {e}"
        )
        return None

    except RequestException as e:
        # Catches connection errors, DNS failures, etc.
        logger.error(f"Failed to fetch Lambda runtime docs: {e}")
        return None
