#!/usr/bin/env python3
"""Select a live, chipset-matching Axiom device for a job payload's meta build.

There is no meta->chipset API in Axiom, so the required chipset is learned by
referencing recent jobs that ran the same softwareProduct and inspecting the
device (hence chipset) they actually used. Candidate devices are then filtered
to live, non-quarantined, non-"Manual" hardware and ranked by freshest heartbeat.

Usage:
    python3 scripts/select_device.py \
        --job-payload-file ./tmp/job_payload.json \
        --env-file .env --out-dir ./tmp \
        [--lookback-days 7] [--heartbeat-max-age 1800] [--taxonomy /APSS/LinuxKernel] \
        [--write-back | --print-only]

Exit codes:
    0 - a device was selected
    2 - could not resolve a chipset for the meta
    3 - chipset resolved but no live device matched
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# Devices whose note matches this are operator-controlled and the pool/auto
# scheduler will not allocate them, even with a live heartbeat (see pool 7818).
MANUAL_NOTE_RE = re.compile(r"manual|offline", re.IGNORECASE)


def load_dotenv(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pick a live chipset-matching device for a meta build.")
    p.add_argument("--job-payload-file", required=True, help="Path to /jobs/submit payload JSON")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--apigee-url", default="https://api-int.qualcomm.com")
    p.add_argument("--taxonomy", help="Override taxonomy path (default: payload 'team')")
    p.add_argument("--lookback-days", type=int, default=7, help="How far back to scan jobs")
    p.add_argument("--heartbeat-max-age", type=int, default=1800, help="Max heartbeat age in seconds")
    p.add_argument("--out-dir", default="./tmp")
    p.add_argument("--app-name", default="AxiomPublicAPI")
    p.add_argument("--client-type", default="automation-script")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--write-back", action="store_true", help="Rewrite payload's resource block in place")
    group.add_argument("--print-only", action="store_true", help="Only print the chosen resource (default)")
    return p.parse_args()


def request_json(
    method: str, url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
    body = None
    req_headers = dict(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = Request(url=url, method=method, headers=req_headers, data=body)
    try:
        with urlopen(req, timeout=120) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}
    except URLError as e:
        return 599, {"error": f"network error: {e}"}


def ensure_token(apigee_url: str) -> str:
    cid = os.environ.get("AXIOM_CLIENT_ID", "").strip()
    csec = os.environ.get("AXIOM_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise RuntimeError("Missing AXIOM_CLIENT_ID / AXIOM_CLIENT_SECRET")
    encoded = base64.b64encode(f"{cid}:{csec}".encode("utf-8")).decode("utf-8")
    url = f"{apigee_url.rstrip('/')}/ent/oauth/v1/accesstoken?grant_type=client_credentials"
    code, data = request_json("POST", url, {"Authorization": f"Basic {encoded}"})
    if code >= 300 or not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(f"Token request failed ({code}): {json.dumps(data)}")
    return data["access_token"]


def common_headers(token: str, app_name: str, client_type: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-QCOM-TracingID": uuid.uuid4().hex,
        "X-QCOM-AppName": app_name,
        "X-QCOM-TokenType": "OAuth",
        "X-QCOM-ClientType": client_type,
    }


def product_from_meta(meta_path: str) -> Optional[str]:
    """`...\\Kaanapali.LA.1.0-01181-STD.TM-1` -> `Kaanapali.LA.1.0`."""
    if not meta_path:
        return None
    segment = meta_path.replace("\\", "/").rstrip("/").split("/")[-1]
    m = re.match(r"^(.*?)-\d", segment)
    return m.group(1) if m else segment or None


def _heartbeat_age_seconds(heartbeat: Optional[str], now: datetime.datetime) -> Optional[float]:
    if not heartbeat:
        return None
    try:
        hbt = datetime.datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - hbt).total_seconds()


def recent_successful_serials(
    base_url: str, headers: Dict[str, str], taxonomy: str, product: str, lookback_days: int
) -> List[str]:
    """Serials of devices that recently ran `product` (most-recent first)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    qs = urlencode(
        {"taxonomyPath": taxonomy, "startDate": start, "endDate": end, "pageNumber": 0, "pageSize": 100}
    )
    code, data = request_json("GET", f"{base_url}/jobs?{qs}", headers)
    if code >= 300 or not isinstance(data, dict):
        return []
    rows = [
        r
        for r in (data.get("data") or [])
        if r.get("softwareProduct") == product and r.get("started")
    ]
    rows.sort(key=lambda r: r.get("started") or "", reverse=True)

    serials: List[str] = []
    for r in rows:
        # List rows often carry the device serial directly.
        for s in r.get("chipIdSerialNumbers") or []:
            if s and s not in serials:
                serials.append(s)
        # Fall back to clone only when the row had no serial.
        if not (r.get("chipIdSerialNumbers")):
            ccode, clone = request_json("GET", f"{base_url}/jobs/{r.get('jobId')}/clone", headers)
            if ccode < 300 and isinstance(clone, dict):
                ident = (clone.get("resource") or {}).get("identifier")
                if ident and ident not in serials:
                    serials.append(ident)
    return serials


def index_devices_by_serial(
    base_url: str, headers: Dict[str, str], taxonomy: str
) -> Dict[str, Dict[str, Any]]:
    qs = urlencode({"taxonomyPath": taxonomy, "type": "Device", "pageNumber": 0, "pageSize": 500})
    code, data = request_json("GET", f"{base_url}/resources?{qs}", headers)
    if code >= 300 or not isinstance(data, dict):
        return {}
    index: Dict[str, Dict[str, Any]] = {}
    for row in data.get("data") or []:
        serial = (row.get("properties") or {}).get("serialNumber")
        if serial:
            index[serial] = row
    return index


def resolve_required_chipset(
    serials: List[str], device_index: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """Chipset of the most-recent successful serial that we can resolve."""
    for serial in serials:
        dev = device_index.get(serial)
        if dev:
            chipset = (dev.get("dependencies") or {}).get("chipset")
            if chipset:
                return chipset
    return None


def rank_devices(
    device_index: Dict[str, Dict[str, Any]],
    required_chipset: str,
    heartbeat_max_age: int,
    recent_serials: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (eligible_ranked, rejected_with_reason)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    recent_set = set(recent_serials)
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for serial, dev in device_index.items():
        dep = dev.get("dependencies") or {}
        chipset = dep.get("chipset")
        note = dev.get("note") or ""
        age = _heartbeat_age_seconds(dev.get("heartbeat"), now)
        row = {
            "serial": serial,
            "id": dev.get("id"),
            "hostname": dev.get("hostname"),
            "chipset": chipset,
            "heartbeat_age_s": None if age is None else int(age),
            "note": note,
        }
        if chipset != required_chipset:
            rejected.append({**row, "reason": f"chipset {chipset} != {required_chipset}"})
            continue
        if dev.get("isQuarantined"):
            rejected.append({**row, "reason": "quarantined"})
            continue
        if age is None:
            rejected.append({**row, "reason": "no heartbeat"})
            continue
        if age >= heartbeat_max_age:
            rejected.append({**row, "reason": f"stale heartbeat ({int(age)}s)"})
            continue
        # The "Manual" note only blocks the *pool* scheduler; a direct type:Device
        # request still runs on it. So a device that demonstrably ran this product
        # within the lookback window (fresh heartbeat already confirmed above)
        # overrides the Manual-note heuristic.
        if MANUAL_NOTE_RE.search(note) and serial not in recent_set:
            rejected.append({**row, "reason": f"manual/offline note: {note!r}"})
            continue
        if MANUAL_NOTE_RE.search(note):
            row["kept_despite_manual"] = True
        eligible.append(row)

    # Freshest heartbeat first; tie-break toward serials seen in recent successful jobs.
    eligible.sort(key=lambda r: (r["serial"] not in recent_set, r["heartbeat_age_s"]))
    return eligible, rejected


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload_path = Path(args.job_payload_file)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    taxonomy = args.taxonomy or payload.get("team")
    meta_path = (payload.get("metaBuild") or {}).get("path", "")
    product = product_from_meta(meta_path)

    if not taxonomy or not product:
        print(f"ERROR: could not derive taxonomy/product (taxonomy={taxonomy}, product={product})",
              file=sys.stderr)
        return 2

    base_url = f"{args.apigee_url.rstrip('/')}/axiom/v1/public"
    headers = common_headers(ensure_token(args.apigee_url), args.app_name, args.client_type)

    serials = recent_successful_serials(base_url, headers, taxonomy, product, args.lookback_days)
    device_index = index_devices_by_serial(base_url, headers, taxonomy)
    required_chipset = resolve_required_chipset(serials, device_index)

    if not required_chipset:
        print(
            f"ERROR: could not resolve a chipset for product '{product}'. No recent successful "
            f"job in the last {args.lookback_days} day(s) used a resolvable device.\n"
            f"Re-run with a larger --lookback-days, or set the resource block manually.",
            file=sys.stderr,
        )
        return 2

    eligible, rejected = rank_devices(device_index, required_chipset, args.heartbeat_max_age, serials)

    selection = {
        "product": product,
        "taxonomy": taxonomy,
        "required_chipset": required_chipset,
        "recent_successful_serials": serials,
        "chosen": eligible[0] if eligible else None,
        "eligible": eligible,
        "rejected": rejected,
    }
    (out_dir / "device_selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")

    print(f"Product           : {product}")
    print(f"Required chipset  : {required_chipset}  (learned from recent successful jobs)")
    print(f"Recent serials    : {serials or '(none found)'}")
    print(f"\nEligible devices  : {len(eligible)}")
    for r in eligible[:10]:
        seen = " *recent" if r["serial"] in set(serials) else ""
        manual = " (manual-note, kept: recently ran product)" if r.get("kept_despite_manual") else ""
        print(f"  {r['serial']:14s} chipset={r['chipset']} host={r['hostname']} hb_age={r['heartbeat_age_s']}s{seen}{manual}")
    if not eligible:
        print("\nNo live device matched. Rejected candidates of the right chipset:")
        for r in rejected:
            if r.get("chipset") == required_chipset:
                print(f"  {r['serial']:14s} -> {r['reason']}")
        print(f"\nERROR: chipset {required_chipset} resolved but no live device available.",
              file=sys.stderr)
        return 3

    chosen_serial = eligible[0]["serial"]
    resource_block = {"type": "Device", "identifier": chosen_serial}
    print(f"\nChosen resource   : {json.dumps(resource_block)}")

    if args.write_back:
        backup = payload_path.with_suffix(payload_path.suffix + ".bak")
        backup.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        payload["resource"] = resource_block
        payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Payload updated   : {payload_path} (backup: {backup})")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
