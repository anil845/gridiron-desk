"""Shared helpers for the ingestion scripts.

Nothing here is specific to a single data source: environment/config loading,
an HTTP GET with exponential backoff and request logging, and a Postgres
connection helper. Keeping it small and dependency-light on purpose — this is a
single-user tool that runs a couple of times a day, not a service.
"""

import logging
import os
import time

import requests

try:
    # Optional: load a local .env if python-dotenv is installed. Not required.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# --- config -----------------------------------------------------------------

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))

# A descriptive UA so the upstream hosts can see who is calling. This is a
# personal, low-volume project; identify it honestly rather than spoofing.
USER_AGENT = os.environ.get(
    "GRIDIRON_USER_AGENT",
    "gridiron-desk/0.1 (personal, non-commercial; single-user)",
)


# --- logging ----------------------------------------------------------------

def get_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger("ingest")


# --- HTTP -------------------------------------------------------------------

def http_get(url, *, expect="json", max_attempts=5, base_delay=2.0, timeout=60,
             stream=False):
    """GET `url` with exponential backoff. Returns parsed JSON, text, or the
    raw `requests.Response` (expect="response", for streaming downloads).

    Retries on network errors and 5xx/429 responses; 4xx (other than 429) fail
    fast because retrying a bad request is pointless. Every attempt is logged.
    """
    if expect not in ("json", "text", "response"):
        raise ValueError("expect must be 'json', 'text', or 'response'")

    headers = {"User-Agent": USER_AGENT}
    attempt = 0
    while True:
        attempt += 1
        try:
            log.info("GET %s (attempt %d/%d)", url, attempt, max_attempts)
            resp = requests.get(url, headers=headers, timeout=timeout, stream=stream)

            if resp.status_code == 429 or resp.status_code >= 500:
                raise _Retryable(f"HTTP {resp.status_code}")
            resp.raise_for_status()

            log.info("  -> %s, %s bytes", resp.status_code,
                     resp.headers.get("Content-Length", "unknown"))
            if expect == "json":
                return resp.json()
            if expect == "text":
                return resp.text
            return resp

        except (_Retryable, requests.exceptions.RequestException) as exc:
            if attempt >= max_attempts:
                log.error("giving up on %s after %d attempts: %s", url, attempt, exc)
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("  retry in %.0fs (%s)", delay, exc)
            time.sleep(delay)


class _Retryable(Exception):
    """Internal marker for a response worth retrying."""


# --- Postgres ---------------------------------------------------------------

def db_connect():
    """Connect using DATABASE_URL, or the standard PG* environment variables if
    DATABASE_URL is unset. Raises if no connection can be made — the ingestion
    scripts need a database to write to.
    """
    import psycopg2  # imported here so a fetch-only dry run needs no driver

    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        conn = psycopg2.connect(dsn)
    else:
        conn = psycopg2.connect()  # falls back to PGHOST/PGUSER/PGDATABASE/...
    conn.autocommit = False
    return conn
