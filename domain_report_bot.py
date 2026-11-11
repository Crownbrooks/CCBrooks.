#!/usr/bin/env python3
"""Generate domain-name ideas and check registration status through RDAP.

RDAP results are informational, not a reservation or purchase. Confirm any
potentially available name with an accredited registrar before acting.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
USER_AGENT = "DomainCandidateScanner/1.0 (public RDAP lookup)"
DEFAULT_TLDS = ("com", "ai", "io", "dev", "tech", "app", "net", "org")
TECH_WORDS = """ai llm lm ml model token agent neural tensor vector embedding prompt inference training compute cloud data graph latent reason vision voice robot secure signal search logic syntax kernel forge spark nexus atlas orbit prism pulse nova quanta cognition intelligence automation language machine learning deep smart future quantum science digital bright clear swift open true prime blue green api bot code chip byte matrix vision learn mind think brain neuron synth cyber pixel robust lucid lumen arc craft bloom datum ether flux gamut horizon ion kinetic merit optic parity query radix sage solve thesis unity vanta wave zenith alpha beta delta echo gamma omega nova axiom catalyst logic insight""".split()
DICTIONARY_WORDS = """amber apple arrow aurora beacon birch blossom brook canyon cedar cipher circle cloud coast comet cosmos craft dawn delta drift eagle earth echo elm ember field finch flame flora forest fox frost galaxy garden glade globe glow grove harbor hazel indigo island jade juniper lake lantern leaf lemon lilac maple meadow meteor mint mirror moon moss mountain muse ocean olive opal orbit orchid otter paper pebble pine planet plum prairie quartz raven river robin rose saffron seed shadow silver sky solstice sparrow spring star stone storm summit sunset sunrise teal terra thistle tide timber valley velvet violet walnut willow wind wren""".split()
BRAND_WORDS = """nexa lumio novara syntra vectra cognia auralis brightwise clearpath cloudwise datawise deepwise evermind fluxora mindwell northstar prismal skylark solara trueflow veritywise""".split()
PAIRS = (
    ("llm", "labs"), ("ai", "forge"), ("neural", "garden"), ("token", "works"),
    ("model", "house"), ("agent", "cloud"), ("vector", "space"), ("latent", "labs"),
    ("prompt", "engine"), ("inference", "hub"), ("tensor", "flow"), ("data", "loom"),
    ("deep", "signal"), ("machine", "garden"), ("bright", "logic"), ("open", "model"),
    ("future", "mind"), ("quantum", "leaf"), ("language", "forge"), ("neural", "nexus"),
    ("model", "orbit"), ("agent", "foundry"), ("vision", "pilot"), ("prompt", "craft"),
    ("reason", "works"), ("signal", "house"), ("swift", "compute"), ("clear", "thought"),
    ("prime", "intelligence"), ("amber", "logic"), ("silver", "cloud"), ("cedar", "labs"),
    ("bright", "harbor"), ("open", "garden"), ("true", "north"), ("lumen", "field"),
)


def clean_label(value: str) -> str | None:
    value = value.strip().lower().replace(" ", "-")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
        return None
    return value


def read_words(path: str | None) -> list[str]:
    words = list(DICTIONARY_WORDS)
    if path:
        with open(path, encoding="utf-8") as source:
            words.extend(line.strip() for line in source if line.strip() and not line.startswith("#"))
    return list(dict.fromkeys(label for word in words if (label := clean_label(word)) and "-" not in label))


def generate_labels(categories: set[str], extra_words: list[str]) -> list[tuple[str, str]]:
    labels: dict[str, str] = {}

    def add(label: str, category: str) -> None:
        valid = clean_label(label)
        if valid:
            labels.setdefault(valid, category)

    if categories & {"llm", "ai", "short-brand", "all"}:
        for word in ("llm", "lm", "ai", "ml", "agi", "gpt", "aix", "nex", "neo", "vox", "zen", "syn", "lum", "ora", "exa", "ion", "arc", "lyra", "nova"):
            add(word, "short-brand")
    if categories & {"tech", "all"}:
        for word in TECH_WORDS + BRAND_WORDS:
            add(word, "tech")
    if categories & {"dictionary", "all"}:
        for word in extra_words:
            add(word, "dictionary")
    if categories & {"two-word", "all"}:
        for first, second in PAIRS:
            add(first + second, "two-word")
            add(first + "-" + second, "two-word")
        firsts = ("ai", "llm", "neural", "token", "model", "agent", "data", "bright", "open", "true")
        seconds = ("labs", "works", "cloud", "forge", "flow", "garden", "signal", "mind", "field", "harbor")
        for first in firsts:
            for second in seconds:
                add(first + second, "two-word")
    return list(labels.items())


def load_bootstrap(timeout: float) -> dict[str, list[str]]:
    request = urllib.request.Request(BOOTSTRAP_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    result: dict[str, list[str]] = {}
    for service in data.get("services", []):
        if len(service) != 2:
            continue
        for suffix in service[0]:
            result[suffix.lower().lstrip(".")] = service[1]
    return result


def rdap_endpoint(domain: str, bootstrap: dict[str, list[str]]) -> str | None:
    labels = domain.lower().rstrip(".").split(".")
    for index in range(len(labels)):
        suffix = ".".join(labels[index:])
        urls = bootstrap.get(suffix)
        if urls:
            base = next((url for url in urls if url.startswith("https://")), urls[0])
            return base.rstrip("/") + "/domain/" + urllib.parse.quote(domain, safe=".")
    return None


def check_domain(domain: str, bootstrap: dict[str, list[str]], timeout: float) -> tuple[str, str]:
    endpoint = rdap_endpoint(domain, bootstrap)
    if not endpoint:
        return "unsupported", "No RDAP service found in IANA bootstrap data for this TLD"
    request = urllib.request.Request(endpoint, headers={"User-Agent": USER_AGENT, "Accept": "application/rdap+json, application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return "registered", "Registry RDAP returned a domain record"
            return "unknown", f"Unexpected registry HTTP status {response.status}"
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "potentially_available", "Registry RDAP returned 404; confirm at a registrar"
        if error.code in (429, 500, 502, 503, 504):
            return "unknown", f"Registry lookup temporarily unavailable (HTTP {error.code})"
        return "unknown", f"Registry RDAP returned HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return "unknown", f"Registry lookup failed: {error}"


def registrar_url(domain: str, registrar: str) -> str:
    quoted = urllib.parse.quote(domain, safe="")
    if registrar == "namecheap":
        return "https://www.namecheap.com/domains/registration/results/?domain=" + quoted
    if registrar == "cloudflare":
        return "https://www.cloudflare.com/products/registrar/"
    return "https://www.godaddy.com/domainsearch/find?domainToCheck=" + quoted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tlds", default=",".join(DEFAULT_TLDS), help="Comma-separated TLDs (default: %(default)s)")
    parser.add_argument("--categories", default="all", help="Comma-separated: llm, ai, short-brand, tech, two-word, dictionary, all")
    parser.add_argument("--wordlist", help="Optional UTF-8 wordlist, one dictionary word per line")
    parser.add_argument("--limit", type=int, default=100, help="Maximum domains checked")
    parser.add_argument("--registrar", choices=("namecheap", "godaddy", "cloudflare"), default="namecheap")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--db", default="seen.db")
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--delay", type=float, default=0.25, help="Delay between requests per worker")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent lookups; maximum 5")
    args = parser.parse_args()
    if args.limit < 1 or args.timeout <= 0 or args.delay < 0 or not 1 <= args.workers <= 5:
        parser.error("limit/timeout must be positive, delay non-negative, and workers between 1 and 5")

    tlds = list(dict.fromkeys(tld.strip().lower().lstrip(".") for tld in args.tlds.split(",") if tld.strip()))
    if not tlds or any(not re.fullmatch(r"[a-z0-9-]{2,63}", tld) or tld.startswith("-") or tld.endswith("-") for tld in tlds):
        parser.error("--tlds must be comma-separated valid TLD labels")
    requested = {name.strip().lower() for name in args.categories.split(",") if name.strip()}
    valid_categories = {"llm", "ai", "short-brand", "tech", "two-word", "dictionary", "all"}
    if not requested or requested - valid_categories:
        parser.error("--categories must use: " + ", ".join(sorted(valid_categories)))
    if "all" in requested:
        requested = {"all"}

    labels = generate_labels(requested, read_words(args.wordlist))
    domains = [(f"{label}.{tld}", category) for label, category in labels for tld in tlds][:args.limit]
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        bootstrap = load_bootstrap(args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print(f"Could not load IANA RDAP bootstrap data: {error}", file=sys.stderr)
        return 2

    def run_one(item: tuple[str, str]) -> dict[str, str]:
        domain, category = item
        if args.delay:
            time.sleep(args.delay)
        status, detail = check_domain(domain, bootstrap, args.timeout)
        return {"domain": domain, "category": category, "status": status, "checked_at": checked_at,
                "registrar_url": registrar_url(domain, args.registrar), "detail": detail}

    results: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, item) for item in domains]
        for future in as_completed(futures):
            results.append(future.result())
    position = {domain: index for index, (domain, _) in enumerate(domains)}
    results.sort(key=lambda row: position[row["domain"]])

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS checks (domain TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, status TEXT NOT NULL)")
        db.executemany("INSERT INTO checks(domain,first_seen,last_checked,status) VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET last_checked=excluded.last_checked,status=excluded.status",
                       [(row["domain"], checked_at, checked_at, row["status"]) for row in results])

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fields = ("domain", "category", "status", "checked_at", "registrar_url", "detail")
    csv_path = output_dir / f"domain-report-{stamp}.csv"
    json_path = output_dir / f"domain-report-{stamp}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    counts = {status: sum(row["status"] == status for row in results) for status in ("potentially_available", "registered", "unknown", "unsupported")}
    print(f"Checked {len(results)} names: {counts['potentially_available']} potentially available; {counts['registered']} registered; {counts['unknown']} unknown; {counts['unsupported']} unsupported.")
    print("RDAP 404 results are not a reservation or guarantee. Confirm exact availability and price with a registrar.")
    for row in results:
        if row["status"] == "potentially_available":
            print(f"{row['domain']} — {row['registrar_url']}")
    summary_path = __import__("os").environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"## Domain scan\n\nChecked **{len(results)}** names: **{counts['potentially_available']} potentially available**, **{counts['registered']} registered**, **{counts['unknown']} unknown**, **{counts['unsupported']} unsupported**.\n\n")
            summary.write("A registry RDAP 404 is only an indication; confirm with a registrar. Names are not reserved.\n\n")
            summary.write("| Domain | Category | RDAP status | Registrar lookup |\n|---|---|---|---|\n")
            for row in results:
                summary.write(f"| `{row['domain']}` | {row['category']} | {row['status']} | [Check]({row['registrar_url']}) |\n")
    print(f"CSV: {csv_path}\nJSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
