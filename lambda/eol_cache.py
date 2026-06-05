"""
eol_cache.py — EOL Data Cache Manager
======================================
Manages reading and writing EOL data to S3.

Caching strategy:
-----------------
Scraping + AI parsing on every Lambda run would be slow and wasteful.
AWS Lambda runtime EOL dates don't change frequently — weekly is plenty.

On each run:
  1. Try to load cached data from S3
  2. If cache exists AND is younger than TTL: return it directly
  3. If cache is missing or stale: scrape fresh, parse with AI, write to cache

Fallback behaviour:
  - If scraping fails, fall back to stale cache rather than aborting.
    Stale EOL data is better than no data — it just might miss very recent changes.
  - If parsing fails, same fallback.
  - Both failures are logged clearly so you can investigate.

Cache location: s3://<eol_cache_bucket>/eol/lambda_runtimes.json
"""

import json
import logging
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError

from eol_scraper import scrape_lambda_runtimes
from ai_parser import parse_eol_data

logger = logging.getLogger(__name__)

# S3 key where the structured Lambda runtime EOL data is stored.
# If you add more check types later, add new cache keys here.
LAMBDA_RUNTIME_CACHE_KEY = "eol/lambda_runtimes.json"


def get_eol_data(config: dict) -> dict | None:
    """
    Returns EOL data from S3 cache if fresh, or scrapes and parses fresh data.

    Parameters:
        config — dict: runtime config from handler.py

    Returns:
        dict: structured EOL data (see ai_parser.py for schema)
        None: if both cache and fresh scrape failed — caller should abort
    """
    bucket = config["eol_cache_bucket"]
    ttl_hours = config["eol_cache_ttl_hours"]

    # Always try the cache first
    cached = _load_from_cache(bucket)

    if cached and _is_fresh(cached, ttl_hours):
        logger.info(
            f"EOL cache is fresh (TTL={ttl_hours}h). Using cached data. "
            f"Scraped at: {cached.get('scraped_at', 'unknown')}"
        )
        return cached

    # Log why we're re-scraping
    if cached:
        logger.info(
            f"EOL cache is stale (TTL={ttl_hours}h exceeded). Re-scraping."
        )
    else:
        logger.info("No EOL cache found. Scraping for the first time.")

    # -----------------------------------------------------------------
    # Cache miss or stale — scrape and re-parse
    # -----------------------------------------------------------------
    raw_html = scrape_lambda_runtimes()

    if not raw_html:
        logger.warning(
            "Scraping failed. "
            + ("Falling back to stale cache." if cached else "No fallback available.")
        )
        # Return stale cache if we have it — better than nothing
        return cached  # May be None

    eol_data = parse_eol_data(raw_html, config)

    if not eol_data:
        logger.warning(
            "AI parsing failed. "
            + ("Falling back to stale cache." if cached else "No fallback available.")
        )
        return cached  # May be None

    # Add timestamp so we know when this data was last refreshed
    eol_data["scraped_at"] = datetime.now(timezone.utc).isoformat()

    # Write to cache — failure here is not fatal, we still return the data
    _write_to_cache(bucket, eol_data)

    return eol_data


def _load_from_cache(bucket: str) -> dict | None:
    """
    Loads and parses the cached EOL JSON from S3.

    Returns:
        dict: parsed cache contents, or None if missing/unreadable
    """
    s3 = boto3.client("s3")

    try:
        response = s3.get_object(Bucket=bucket, Key=LAMBDA_RUNTIME_CACHE_KEY)
        content = response["Body"].read().decode("utf-8")
        data = json.loads(content)
        logger.info("Cache loaded from S3 successfully.")
        return data

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "NoSuchKey":
            # Expected on first run — not an error
            logger.info("Cache miss: no EOL data file found in S3 yet.")
        else:
            logger.warning(f"Unexpected S3 error loading cache: {e}")

        return None

    except json.JSONDecodeError as e:
        # Cache file exists but is corrupt
        logger.warning(f"Cache file exists but contains invalid JSON: {e}")
        return None


def _is_fresh(cached_data: dict, ttl_hours: int) -> bool:
    """
    Returns True if the cached data is within the TTL window.

    Parameters:
        cached_data — dict: must contain a "scraped_at" ISO timestamp key
        ttl_hours   — int: max age in hours before cache is considered stale

    Returns:
        bool
    """
    scraped_at_str = cached_data.get("scraped_at")

    if not scraped_at_str:
        logger.warning("Cache is missing 'scraped_at' timestamp. Treating as stale.")
        return False

    try:
        scraped_at = datetime.fromisoformat(scraped_at_str)

        # Ensure timezone awareness for comparison
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - scraped_at
        max_age = timedelta(hours=ttl_hours)
        fresh = age < max_age

        logger.info(
            f"Cache age: {age}. Max allowed: {max_age}. Fresh: {fresh}"
        )
        return fresh

    except ValueError as e:
        logger.warning(f"Could not parse cache timestamp '{scraped_at_str}': {e}")
        return False


def _write_to_cache(bucket: str, eol_data: dict) -> None:
    """
    Writes structured EOL data to S3 as formatted JSON.

    Parameters:
        bucket   — str: the S3 bucket name
        eol_data — dict: the data to cache

    A write failure is logged but not raised — the caller still gets
    fresh data for this run, and the next run will try again.
    """
    s3 = boto3.client("s3")

    try:
        s3.put_object(
            Bucket=bucket,
            Key=LAMBDA_RUNTIME_CACHE_KEY,
            Body=json.dumps(eol_data, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(
            f"EOL data cached to s3://{bucket}/{LAMBDA_RUNTIME_CACHE_KEY}"
        )

    except ClientError as e:
        # Cache write failing is inconvenient but not critical.
        # The Lambda will try to refresh again on the next run.
        logger.warning(f"Failed to write EOL data to cache: {e}")
