#!/usr/bin/env python3
"""Sync TLSA DNS records from Stalwart to Cloudflare.

Environment variables (required unless passed via flags):
  DOMAIN_NAME
  CF_API_KEY                 (Cloudflare API token; used as Bearer token)
  STALWART_API_KEY           (Stalwart API key; used as Bearer token)
  STALWART_ENDPOINT_URL      (default: http://stalwart:8080)

Optional environment variables:
  CF_PER_PAGE                (default: 100)
  CF_TTL                     (default: 120)

Additional pre-sync environment variables (optional):
  DISCORD_WEBHOOK            (If set, post status messages to Discord)
  DISCORD_USERNAME           (Discord username override; default: Stalwart)
  STALWART_CLI_PATH          (Path to stalwart-cli; default: /opt/bin/stalwart-cli)

Examples:
  DOMAIN_NAME=example.com \
  CF_API_KEY=... \
  STALWART_API_KEY=... \
  STALWART_ENDPOINT_URL=http://stalwart:8080 \
  ./stalwart.py

Notes:
  - Uses only Python stdlib (no requests dependency).
  - Compares normalized sets of (name, content) pairs.
  - Applies minimal changes: deletes stale Cloudflare TLSA and adds missing ones.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class NormTLSA:
    name: str
    content: str  # "usage selector matching_type certificate"


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def env_required(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise ValueError(f"Missing required environment variable: {key}")
    return val


def http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Sequence[str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method)
    for h in headers or []:
        req.add_header(*h.split(": ", 1))

    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as ex:
        # Try to include JSON error details if available
        detail = ""
        try:
            detail = ex.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {ex.code} for {method} {url}: {detail}") from ex
    except urllib.error.URLError as ex:
        raise RuntimeError(f"Network error for {method} {url}: {ex}") from ex

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as ex:
        snippet = raw[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Invalid JSON response from {url}: {ex}; body starts: {snippet!r}") from ex


def normalize_name(name: str) -> str:
    return name.strip().lower().rstrip(".")


def normalize_tlsa_records(records: Iterable[Dict[str, Any]]) -> List[NormTLSA]:
    out: List[NormTLSA] = []
    for r in records:
        name = normalize_name(str(r.get("name", "")))
        content = str(r.get("content", "")).strip()
        if not name or not content:
            continue

        parts = [p.strip() for p in content.split() if p.strip()]
        parts = parts[:4]
        if len(parts) != 4:
            # Skip invalid TLSA formats but keep going
            eprint(f"WARN: Skipping invalid TLSA content for {name}: {content!r}")
            continue

        out.append(NormTLSA(name=name, content=" ".join(parts)))

    # Stable ordering for consistent logs
    out.sort(key=lambda x: (x.name, x.content))
    return out


def tlsa_set(records: Iterable[NormTLSA]) -> Set[Tuple[str, str]]:
    return {(r.name, r.content) for r in records}


def stalwart_fetch_tlsa(domain: str, endpoint_url: str, stalwart_key: str) -> List[NormTLSA]:
    base = endpoint_url.rstrip("/")
    url = f"{base}/api/dns/records/{urllib.parse.quote(domain)}"
    resp = http_json(url, "GET", headers=[f"Authorization: Bearer {stalwart_key}"])

    data = resp.get("data")
    if not isinstance(data, list):
        return []

    tlsa = [r for r in data if str(r.get("type", "")).upper() == "TLSA"]
    # Stalwart seems to return fields {type, name, content, ...}
    return normalize_tlsa_records(tlsa)


def cloudflare_get_zone_id(domain: str, cf_key: str) -> str:
    params = urllib.parse.urlencode({"name": domain, "status": "active"})
    url = f"https://api.cloudflare.com/client/v4/zones?{params}"
    resp = http_json(url, "GET", headers=[f"Authorization: Bearer {cf_key}"])

    if not resp.get("success"):
        errors = resp.get("errors") or []
        msg = errors[0].get("message") if errors else "unknown error"
        raise RuntimeError(f"Cloudflare zone lookup failed: {msg}")

    result = resp.get("result")
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"Zone not found for domain {domain}")

    zone_id = result[0].get("id")
    if not zone_id:
        raise RuntimeError("Cloudflare returned zone result without id")
    return str(zone_id)


def cloudflare_list_dns_records(zone_id: str, cf_key: str, record_type: Optional[str], per_page: int) -> List[Dict[str, Any]]:
    # Cloudflare paginates; iterate until no more pages
    records: List[Dict[str, Any]] = []
    page = 1
    while True:
        qs = {"per_page": str(per_page), "page": str(page)}
        if record_type:
            qs["type"] = record_type
        url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?{urllib.parse.urlencode(qs)}"
        resp = http_json(url, "GET", headers=[f"Authorization: Bearer {cf_key}"])
        if not resp.get("success"):
            errors = resp.get("errors") or []
            msg = errors[0].get("message") if errors else "unknown error"
            raise RuntimeError(f"Cloudflare dns_records list failed: {msg}")

        result = resp.get("result")
        if isinstance(result, list):
            records.extend(result)

        info = resp.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return records


def cloudflare_existing_tlsa(zone_id: str, cf_key: str, per_page: int) -> Tuple[List[NormTLSA], Dict[Tuple[str, str], List[str]]]:
    raw = cloudflare_list_dns_records(zone_id, cf_key, record_type="TLSA", per_page=per_page)

    norm: List[NormTLSA] = []
    ids_by_key: Dict[Tuple[str, str], List[str]] = {}

    for r in raw:
        name = normalize_name(str(r.get("name", "")))
        rid = str(r.get("id", ""))
        data = r.get("data") or {}

        # Cloudflare TLSA shape: data: {usage, selector, matching_type, certificate}
        usage = str(data.get("usage", "")).strip()
        selector = str(data.get("selector", "")).strip()
        matching = str(data.get("matching_type", "")).strip()
        cert = str(data.get("certificate", "")).strip()

        if not (name and usage and selector and matching and cert and rid):
            continue

        content = f"{usage} {selector} {matching} {cert}".strip()
        rec = NormTLSA(name=name, content=content)
        norm.append(rec)
        ids_by_key.setdefault((rec.name, rec.content), []).append(rid)

    norm.sort(key=lambda x: (x.name, x.content))
    return norm, ids_by_key


def cloudflare_delete_record(zone_id: str, record_id: str, cf_key: str) -> None:
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    resp = http_json(url, "DELETE", headers=[f"Authorization: Bearer {cf_key}", "Content-Type: application/json"])
    if not resp.get("success"):
        errors = resp.get("errors") or []
        msg = errors[0].get("message") if errors else "unknown error"
        raise RuntimeError(f"Delete failed for record {record_id}: {msg}")


def cloudflare_add_tlsa(zone_id: str, cf_key: str, rec: NormTLSA, ttl: int) -> None:
    parts = rec.content.split(" ", 3)
    if len(parts) != 4:
        raise ValueError(f"Invalid TLSA content for {rec.name}: {rec.content!r}")
    usage, selector, matching_type, certificate = parts

    payload = {
        "type": "TLSA",
        "name": rec.name,
        "data": {
            "usage": int(usage),
            "selector": int(selector),
            "matching_type": int(matching_type),
            "certificate": certificate,
        },
        "proxied": False,
        "ttl": int(ttl),
    }

    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    body = json.dumps(payload).encode("utf-8")
    resp = http_json(url, "POST", headers=[f"Authorization: Bearer {cf_key}", "Content-Type: application/json"], body=body)
    if not resp.get("success"):
        errors = resp.get("errors") or []
        msg = errors[0].get("message") if errors else "unknown error"
        raise RuntimeError(f"Add failed for {rec.name}: {msg}")


def _post_discord(webhook_url: str, username: str, content: str, timeout_s: float = 10.0) -> None:
    """Best-effort Discord webhook post. Errors are logged but not fatal."""
    payload = {"username": username, "content": content}
    body = json.dumps(payload).encode("utf-8")
    try:
        http_json(
            webhook_url,
            "POST",
            headers=["Content-Type: application/json"],
            body=body,
            timeout=int(timeout_s),
        )
    except Exception as e:
        log(f"[warn] Discord webhook post failed: {e}")


def reload_stalwart_certificates_from_env(dry_run: bool = False) -> None:
    """
    Reload Stalwart certificates before DNS sync.

    Uses the same two env vars the sync already requires:
      - STALWART_ENDPOINT_URL
      - STALWART_API_KEY

    Runs:
      $STALWART_CLI_PATH -u $STALWART_ENDPOINT_URL -c $STALWART_API_KEY server reload-certificates

    If DISCORD_WEBHOOK is set, it also posts a status message first.
    """
    msg = "Reloading Stalwart certificates..."
    discord_webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    discord_username = os.environ.get("DISCORD_USERNAME", "Stalwart").strip() or "Stalwart"
    if discord_webhook:
        _post_discord(discord_webhook, discord_username, msg)

    cli_path = os.environ.get("STALWART_CLI_PATH", "/opt/bin/stalwart-cli").strip() or "/opt/bin/stalwart-cli"

    endpoint_url = os.environ.get("STALWART_ENDPOINT_URL", "http://stalwart:8080").strip() or "http://stalwart:8080"
    api_key = os.environ.get("STALWART_API_KEY", "").strip()

    # Treat reload as optional if not enough info was provided
    if not endpoint_url or not api_key:
        log("[info] Skipping certificate reload (STALWART_ENDPOINT_URL and/or STALWART_API_KEY not set).")
        return

    log(msg)
    cmd = [cli_path, "-u", endpoint_url, "-c", api_key, "server", "reload-certificates"]
    if dry_run:
        log(f"[dry-run] Would run: {' '.join(cmd)}")
        return

    try:
        import subprocess
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        raise SystemExit(
            f"stalwart-cli not found at '{cli_path}'. Ensure it exists in the image or set STALWART_CLI_PATH."
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"stalwart-cli failed with exit code {e.returncode}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Sync TLSA records from Stalwart to Cloudflare.")
    p.add_argument("--domain", default=os.getenv("DOMAIN_NAME"), help="Domain name (or env DOMAIN_NAME)")
    p.add_argument("--cf-api-key", default=os.getenv("CF_API_KEY"), help="Cloudflare API token (or env CF_API_KEY)")
    p.add_argument("--stalwart-api-key", default=os.getenv("STALWART_API_KEY"), help="Stalwart API key (or env STALWART_API_KEY)")
    p.add_argument("--stalwart-endpoint-url", default=os.getenv("STALWART_ENDPOINT_URL", "http://stalwart:8080"), help="Stalwart base URL (or env STALWART_ENDPOINT_URL)")
    p.add_argument("--dry-run", action="store_true", help="Show planned changes but do not modify Cloudflare")
    p.add_argument("--verbose", action="store_true", help="Print normalized record sets")
    args = p.parse_args(argv)

    # Pre-sync: reload Stalwart certificates before reading TLSA records / updating DNS.
    reload_stalwart_certificates_from_env(dry_run=args.dry_run)

    try:
        domain = (args.domain or "").strip()
        cf_key = (args.cf_api_key or "").strip()
        stalwart_key = (args.stalwart_api_key or "").strip()
        stalwart_url = (args.stalwart_endpoint_url or "").strip()
        if not domain:
            raise ValueError("Missing domain (flag --domain or env DOMAIN_NAME)")
        if not cf_key:
            raise ValueError("Missing Cloudflare key (flag --cf-api-key or env CF_API_KEY)")
        if not stalwart_key:
            raise ValueError("Missing Stalwart key (flag --stalwart-api-key or env STALWART_API_KEY)")
        if not stalwart_url:
            raise ValueError("Missing Stalwart URL (flag --stalwart-endpoint-url or env STALWART_ENDPOINT_URL)")

        per_page = int(os.getenv("CF_PER_PAGE", "100"))
        ttl = int(os.getenv("CF_TTL", "120"))

        print(f"Starting TLSA record synchronization for {domain}...")

        print("Fetching TLSA records from Stalwart...")
        desired = stalwart_fetch_tlsa(domain, stalwart_url, stalwart_key)
        if not desired:
            print("No TLSA records found in Stalwart. Exiting.")
            return 0
        print(f"Found {len(desired)} TLSA records from Stalwart.")

        print(f"Fetching Cloudflare zone ID for {domain}...")
        zone_id = cloudflare_get_zone_id(domain, cf_key)
        print(f"Zone ID: {zone_id}.")

        print("Fetching existing TLSA records from Cloudflare...")
        existing, ids_by_key = cloudflare_existing_tlsa(zone_id, cf_key, per_page=per_page)
        print(f"Found {len(existing)} TLSA records in Cloudflare.")

        desired_set = tlsa_set(desired)
        existing_set = tlsa_set(existing)

        if args.verbose:
            print("\nNormalized Stalwart records:")
            for r in desired:
                print(f"- {r.name} ({r.content})")
            print("\nNormalized Cloudflare records:")
            for r in existing:
                print(f"- {r.name} ({r.content})")

        if desired_set == existing_set:
            print("\nTLSA records are already up to date.")
            return 0

        to_delete = existing_set - desired_set
        to_add = desired_set - existing_set

        print("\nPlanned changes:")
        if to_delete:
            print(f"- Delete {len(to_delete)} stale Cloudflare TLSA record(s)")
        else:
            print("- Delete 0 stale Cloudflare TLSA record(s)")
        if to_add:
            print(f"- Add {len(to_add)} missing TLSA record(s)")
        else:
            print("- Add 0 missing TLSA record(s)")

        if args.dry_run:
            print("\nDry-run enabled: no changes applied.")
            return 0

        # Apply deletes first
        if to_delete:
            print("\nDeleting stale Cloudflare TLSA records...")
            for name, content in sorted(to_delete):
                ids = ids_by_key.get((name, content), [])
                if not ids:
                    # Shouldn't happen, but keep going
                    eprint(f"WARN: No record id found for {name} ({content})")
                    continue
                for rid in ids:
                    cloudflare_delete_record(zone_id, rid, cf_key)
                    print(f"Deleted: {name} ({content})")
                    # small delay to avoid API burst issues
                    time.sleep(0.05)

        if to_add:
            print("\nAdding missing TLSA records to Cloudflare...")
            for name, content in sorted(to_add):
                rec = NormTLSA(name=name, content=content)
                cloudflare_add_tlsa(zone_id, cf_key, rec, ttl=ttl)
                print(f"Added: {name} ({content})")
                time.sleep(0.05)

        print("\nTLSA record synchronization completed.")
        return 0

    except Exception as ex:
        eprint(f"ERROR: {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
