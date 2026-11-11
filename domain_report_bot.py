#!/usr/bin/env python3
"""Generate and check a curated set of tech-focused domain names via RDAP.

This only checks public registration data. It does not reserve, buy, or activate
names. Use the registrar links in the report to complete any purchase yourself.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RDAP_BASE = "https://rdap.org/domain/"
DEFAULT_TLDS = ("com", "ai", "io", "dev", "tech")
# Short AI/ML terms plus familiar dictionary words suitable for brand ideas.
SINGLES = (
    "llm", "lm", "model", "token", "agent", "neural", "tensor", "vector",
    "embedding", "prompt", "inference", "training", "compute", "cloud",
    "data", "graph", "latent", "reason", "vision", "voice", "robot",
    "secure", "signal", "search", "logic", "syntax", "kernel", "forge",
    "spark", "nexus", "atlas", "orbit", "prism", "pulse", "nova", "quanta",
    "cognition", "intelligence", "automation", "language", "machine",
    "learning", "deep", "smart", "future", "quantum", "science", "digital",
    "bright", "clear", "swift", "open", "true", "prime", "blue", "green",
)
PAIRS = (
    ("llm", "labs"), ("lm", "labs"), ("neural", "forge"),
    ("token", "works"), ("model", "house"), ("agent", "cloud"),
    ("vector", "space"), ("latent", "labs"), ("prompt", "engine"),
    ("inference", "hub"), ("tensor", "flow"), ("data", "loom"),
    ("deep", "signal"), ("machine", "garden"), ("bright", "logic"),
    ("open", "model"), ("future", "mind"), ("quantum", "leaf"),
    ("cognitive", "spark"), ("language", "forge"), ("neural", "nexus"),
    ("model", "orbit"), ("agent", "foundry"), ("vision", "pilot"),
    ("prompt", "craft"), ("reason", "works"), ("signal", "house"),
    ("swift", "compute"), ("clear", "thought"), ("prime", "intelligence"),
)


def candidates(tlds: tuple[str, ...]) -> list[str]:
    """Return unique candidates, preferring short tech terms and then pairs."""
    labels = list(dict.fromkeys(SINGLES))
    labels.extend("".join(pair) for pair in PAIRS)
    return [f"{label}.{tld}" for label in labels for tld in tlds]


def check_domain(domain: str, timeout: float = 15.0) -> tuple[str, str]:
    """Return (status, detail), conservatively distinguishing errors from free names."""
    url = RDAP_BASE + urllib.parse.quote(domain, safe=".")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/rdap+json", "User-Agent": "DomainReportBot/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return "registered", "RDAP record found"
            return "unknown", f"Unexpected HTTP status {response.status}"
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "available", "No RDAP registration record (verify with a registrar)"
        return "unknown", f"RDAP HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return "unknown", f"Lookup failed: {error}"


def open_chrome(url: str) -> bool:
    """Open a registrar lookup in Chrome when installed; never auto-purchase."""
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if sys.platform == "darwin":
        if shutil.which("open"):
            command = ["open", "-a", "Google Chrome", url]
        else:
            command = [chrome, url] if chrome else []
    elif os.name == "nt":
        chrome = chrome or shutil.which("chrome")
        command = [chrome, url] if chrome else []
    else:
        command = [chrome, url] if chrome else []
    if not command:
        return False
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run the normal scan (default)")
    mode.add_argument("--test", action="store_true", help="Run a small scan for up to two minutes")
    parser.add_argument("--open-chrome", action="store_true", help="Open available-name registrar links in Chrome")
    parser.add_argument("--limit", type=int, default=30, help="Maximum names to check (default: 30)")
    parser.add_argument("--tlds", default=",".join(DEFAULT_TLDS), help="Comma-separated TLDs")
    parser.add_argument("--output-dir", default="reports", help="Directory for CSV and JSON reports")
    parser.add_argument("--db", default="seen.db", help="SQLite history database")
    args = parser.parse_args()

    tlds = tuple(dict.fromkeys(tld.strip().lower().lstrip(".") for tld in args.tlds.split(",") if tld.strip()))
    if not tlds or any(not tld.replace("-", "").isalnum() for tld in tlds):
        parser.error("--tlds must contain valid TLD labels")
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    max_names = min(args.limit, 5) if args.test else args.limit
    deadline = time.monotonic() + 120 if args.test else None
    scan = candidates(tlds)[:max_names]
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, str]] = []

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS checks (domain TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, status TEXT NOT NULL)")
        for index, domain in enumerate(scan):
            if deadline is not None and time.monotonic() >= deadline:
                break
            status, detail = check_domain(domain)
            registrar_url = "https://www.namecheap.com/domains/registration/results/?domain=" + urllib.parse.quote(domain)
            row = {"domain": domain, "status": status, "checked_at": checked_at, "registrar_url": registrar_url, "detail": detail}
            rows.append(row)
            db.execute(
                "INSERT INTO checks(domain, first_seen, last_checked, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET last_checked=excluded.last_checked, status=excluded.status",
                (domain, checked_at, checked_at, status),
            )
            if args.open_chrome and status == "available":
                if not open_chrome(registrar_url):
                    print("Chrome is unavailable; use registrar links in the report.", file=sys.stderr)
            # Keep requests gentle and avoid hammering the public RDAP service.
            if index + 1 < len(scan):
                time.sleep(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"domain-report-{stamp}.csv"
    json_path = output_dir / f"domain-report-{stamp}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("domain", "status", "checked_at", "registrar_url", "detail"))
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

    available = [row for row in rows if row["status"] == "available"]
    unknown = sum(row["status"] == "unknown" for row in rows)
    print(f"Checked {len(rows)} domains: {len(available)} potentially available, {unknown} unknown.")
    print("RDAP results are indicative only; confirm availability and price with a registrar.")
    for row in available:
        print(f"AVAILABLE? {row['domain']} — {row['registrar_url']}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"## Domain report\n\nChecked **{len(rows)}** domains; **{len(available)}** potentially available; **{unknown}** unknown.\n\n")
            summary.write("RDAP is indicative only; confirm availability and price with a registrar. Names are not reserved.\n\n")
            if available:
                summary.write("| Candidate | Registrar lookup |\n|---|---|\n")
                for row in available:
                    summary.write(f"| `{row['domain']}` | [Check registrar]({row['registrar_url']}) |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
