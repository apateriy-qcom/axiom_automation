#!/usr/bin/env python3
"""Watch a job for host/device assignment and run host connectivity checks."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
    p = argparse.ArgumentParser(
        description="Poll Axiom job assignment, notify by mail, and test host connectivity."
    )
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--env-file", default=".env")
    p.add_argument("--apigee-url", default="https://api-int.qualcomm.com")
    p.add_argument("--taxonomy", default="/APSS/LinuxKernel")
    p.add_argument("--poll-interval", type=int, default=20)
    p.add_argument("--max-wait", type=int, default=3600)
    p.add_argument("--out-dir", default="./tmp")
    p.add_argument("--notify-email", action="append", default=[])
    p.add_argument("--app-name", default="AxiomPublicAPI")
    p.add_argument("--client-type", default="automation-script")
    p.add_argument(
        "--report-file",
        help="Optional markdown report path. Default: <out-dir>/job_watch_<job-id>.report.md",
    )
    p.add_argument(
        "--resource-id",
        type=int,
        help="Optional resource ID to query for serial/adbId enrichment (GET /resources/{id}).",
    )
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
            code = resp.getcode()
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8", errors="replace")
    except URLError as e:
        return 599, {"error": f"network error: {e}"}

    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"raw": raw}
    return code, data


def ensure_token(apigee_url: str) -> str:
    cid = os.environ.get("AXIOM_CLIENT_ID", "").strip()
    csec = os.environ.get("AXIOM_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise RuntimeError("Missing AXIOM_CLIENT_ID / AXIOM_CLIENT_SECRET")
    encoded = base64.b64encode(f"{cid}:{csec}".encode("utf-8")).decode("utf-8")
    url = f"{apigee_url.rstrip('/')}/ent/oauth/v1/accesstoken?grant_type=client_credentials"
    code, data = request_json("POST", url, {"Authorization": f"Basic {encoded}"})
    if code >= 300:
        raise RuntimeError(f"Token request failed ({code}): {json.dumps(data)}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in token response: {json.dumps(data)}")
    return token


def common_headers(token: str, app_name: str, client_type: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-QCOM-TracingID": uuid.uuid4().hex,
        "X-QCOM-AppName": app_name,
        "X-QCOM-TokenType": "OAuth",
        "X-QCOM-ClientType": client_type,
    }


def run_cmd(args: Sequence[str], timeout: int = 12) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"cmd": " ".join(shlex.quote(x) for x in args), "available": False}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(shlex.quote(x) for x in args), "timeout": True}
    return {
        "cmd": " ".join(shlex.quote(x) for x in args),
        "available": True,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


def extract_signals(results: Dict[str, Any]) -> Dict[str, Any]:
    rows = results.get("data") or []

    hosts = sorted(
        {
            str(v)
            for row in rows
            for v in [row.get("playlistHostName"), row.get("testCaseHostName")]
            if v
        }
    )
    test_resources = sorted(
        {
            str(v)
            for row in rows
            for v in [row.get("playlistTestResource"), row.get("testCaseTestResourceName")]
            if v
        }
    )
    serial_keys = sorted({k for row in rows for k in row.keys() if "serial" in k.lower()})
    serial_values = sorted(
        {str(row.get(k)) for row in rows for k in serial_keys if row.get(k)}
    )

    adb_pattern = re.compile(r"(?i)(?:adb[^0-9]{0,20}(\d{4,5})|(\d{4,5})[^0-9]{0,20}adb)")
    adb_ports = set()
    adb_keys = set()
    for row in rows:
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if "adb" not in value.lower() and "port" not in key.lower():
                continue
            adb_keys.add(key)
            for m in adb_pattern.finditer(value):
                port = m.group(1) or m.group(2)
                if port:
                    adb_ports.add(port)

    return {
        "results_rows": len(rows),
        "hosts": hosts,
        "test_resources": test_resources,
        "serial_keys": serial_keys,
        "serial_values": serial_values,
        "adb_keys": sorted(adb_keys),
        "adb_ports": sorted(adb_ports),
    }


def host_connectivity_checks(host: str) -> Dict[str, Any]:
    checks: Dict[str, Any] = {"host": host}
    checks["ping"] = run_cmd(["ping", "-c", "1", "-W", "2", host])
    checks["rdp_3389"] = run_cmd(["nc", "-vz", "-w", "3", host, "3389"])
    checks["smb_445"] = run_cmd(["nc", "-vz", "-w", "3", host, "445"])

    win_user = os.environ.get("WINDOWS_USER", "").strip()
    win_pass = os.environ.get("WINDOWS_PASS", "").strip()
    win_domain = os.environ.get("WINDOWS_DOMAIN", "").strip()
    if win_user and win_pass:
        user_spec = f"{win_domain}\\{win_user}%{win_pass}" if win_domain else f"{win_user}%{win_pass}"
        checks["smb_auth"] = run_cmd(
            ["smbclient", "-L", f"//{host}", "-U", user_spec, "-m", "SMB3"], timeout=20
        )
    else:
        checks["smb_auth"] = {
            "skipped": True,
            "reason": "Set WINDOWS_USER and WINDOWS_PASS (optional WINDOWS_DOMAIN) to test authenticated access.",
        }
    return checks


def try_send_mail(recipients: List[str], subject: str, body: str) -> Dict[str, Any]:
    if not recipients:
        return {"sent": False, "reason": "no recipients"}
    cmd = ["mail", "-s", subject, ",".join(recipients)]
    try:
        proc = subprocess.run(cmd, input=body, text=True, capture_output=True, check=False, timeout=15)
    except FileNotFoundError:
        return {"sent": False, "reason": "mail command not found"}
    except subprocess.TimeoutExpired:
        return {"sent": False, "reason": "mail command timeout"}
    return {
        "sent": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stderr": proc.stderr[-500:],
    }


def write_report(path: Path, snapshot: Dict[str, Any]) -> None:
    signals = snapshot.get("signals") or {}
    conn = snapshot.get("host_connectivity") or {}
    resource = snapshot.get("resource_lookup") or {}
    lines = [
        "# Job Assignment / Host Connectivity Report",
        "",
        f"- UTC timestamp: `{snapshot.get('ts_utc')}`",
        f"- Job ID: `{snapshot.get('job_id')}`",
        f"- Job state: `{snapshot.get('job_state')}`",
        f"- Job setup state: `{snapshot.get('job_setup_state')}`",
        "",
        "## Steps",
        "",
        "1. Polled `/jobs/{id}/info` and `/jobs/{id}/results`.",
        "2. Detected assignment host/resource from result metadata.",
        "3. Tested workspace connectivity to assigned Windows host (ping, RDP 3389, SMB 445).",
        "4. Performed SMB auth/listing check when credentials are available.",
        "5. Parsed serial/ADB indicators from results and resource lookup.",
        "6. Sent email notification on assignment / ADB port detection (if configured).",
        "",
        "## Assignment Signals",
        "",
        f"- Hosts: `{signals.get('hosts')}`",
        f"- Test resources: `{signals.get('test_resources')}`",
        f"- Serial values in results: `{signals.get('serial_values')}`",
        f"- ADB ports in results: `{signals.get('adb_ports')}`",
        "",
        "## Host Connectivity",
        "",
        f"- Host: `{conn.get('host')}`",
        f"- Ping exit code: `{(conn.get('ping') or {}).get('exit_code')}`",
        f"- TCP 3389 exit code: `{(conn.get('rdp_3389') or {}).get('exit_code')}`",
        f"- TCP 445 exit code: `{(conn.get('smb_445') or {}).get('exit_code')}`",
        f"- SMB auth result: `{conn.get('smb_auth')}`",
        "",
        "## Resource Lookup",
        "",
        f"- Resource ID: `{resource.get('id')}`",
        f"- Serial: `{resource.get('serial')}`",
        f"- Hostname: `{resource.get('hostname')}`",
        f"- adbId values: `{resource.get('adbId')}`",
        "",
        "## Notifications",
        "",
        f"- Assignment mail: `{snapshot.get('mail_assignment')}`",
        f"- ADB mail: `{snapshot.get('mail_adb')}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_headers(args: argparse.Namespace) -> Tuple[str, Dict[str, str]]:
    token = ensure_token(args.apigee_url)
    return token, common_headers(token, args.app_name, args.client_type)


def lookup_resource(
    base_url: str, headers: Dict[str, str], resource_id: Optional[int], serial_hint: str
) -> Dict[str, Any]:
    if resource_id is not None:
        code, data = request_json("GET", f"{base_url}/resources/{resource_id}", headers)
        if code < 300 and isinstance(data, dict):
            props = data.get("properties") or {}
            return {
                "id": data.get("id"),
                "serial": props.get("serialNumber"),
                "hostname": data.get("hostname"),
                "adbId": props.get("adbId"),
                "httpCode": code,
            }
        return {"httpCode": code, "error": data}

    if not serial_hint:
        return {"skipped": True, "reason": "no resource-id or serial hint"}

    # Resolve by serial via resources list in taxonomy.
    code, data = request_json(
        "GET",
        f"{base_url}/resources?taxonomyPath=/APSS/LinuxKernel&type=Device&pageNumber=0&pageSize=500",
        headers,
    )
    if code >= 300 or not isinstance(data, dict):
        return {"httpCode": code, "error": data}
    rows = data.get("data") or []
    for row in rows:
        props = row.get("properties") or {}
        if str(props.get("serialNumber") or "").strip() == serial_hint:
            return {
                "id": row.get("id"),
                "serial": props.get("serialNumber"),
                "hostname": row.get("hostname"),
                "adbId": props.get("adbId"),
                "httpCode": 200,
            }
    return {"httpCode": 404, "error": f"serial not found: {serial_hint}"}


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_url = f"{args.apigee_url.rstrip('/')}/axiom/v1/public"
    _, headers = refresh_headers(args)

    request_json("PUT", f"{base_url}/users/updatepermission", headers)
    report_file = (
        Path(args.report_file)
        if args.report_file
        else out_dir / f"job_watch_{args.job_id}.report.md"
    )

    started = time.time()
    notified_assignment = False
    notified_adb = False
    last_snapshot: Dict[str, Any] = {}
    event_data: Dict[str, Any] = {}

    while True:
        info_code, info = request_json("GET", f"{base_url}/jobs/{args.job_id}/info", headers)
        results_code, results = request_json(
            "GET", f"{base_url}/jobs/{args.job_id}/results?pageNumber=0&pageSize=200", headers
        )
        # Token may expire for long-running watchers. Refresh once on auth failures.
        if info_code == 401 or results_code == 401:
            _, headers = refresh_headers(args)
            info_code, info = request_json("GET", f"{base_url}/jobs/{args.job_id}/info", headers)
            results_code, results = request_json(
                "GET", f"{base_url}/jobs/{args.job_id}/results?pageNumber=0&pageSize=200", headers
            )

        signals = extract_signals(results if isinstance(results, dict) else {})
        snapshot: Dict[str, Any] = {
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "job_id": args.job_id,
            "info_http": info_code,
            "results_http": results_code,
            "job_state": (info or {}).get("state"),
            "job_setup_state": (info or {}).get("jobSetupState"),
            "submitted": (info or {}).get("submitted"),
            "started": (info or {}).get("started"),
            "ended": (info or {}).get("ended"),
            "signals": signals,
        }
        if event_data:
            snapshot.update(event_data)
        last_snapshot = snapshot
        out_file = out_dir / f"job_watch_{args.job_id}.latest.json"
        out_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        write_report(report_file, snapshot)

        has_assignment = bool(signals["hosts"] or signals["test_resources"])
        has_adb = bool(signals["adb_ports"])

        if has_assignment and not notified_assignment:
            host = signals["hosts"][0] if signals["hosts"] else ""
            connectivity = host_connectivity_checks(host) if host else {"skipped": True}
            serial_hint = ""
            for resource in signals["test_resources"]:
                if resource and not str(resource).startswith("{"):
                    serial_hint = str(resource).strip()
                    break
            resource_lookup = lookup_resource(base_url, headers, args.resource_id, serial_hint)
            event_data["host_connectivity"] = connectivity
            event_data["resource_lookup"] = resource_lookup
            snapshot.update(event_data)
            out_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            write_report(report_file, snapshot)

            body = (
                f"Job {args.job_id} has assignment.\n\n"
                f"State: {snapshot['job_state']}\n"
                f"Host(s): {signals['hosts']}\n"
                f"Resource(s): {signals['test_resources']}\n"
                f"Serial(s): {signals['serial_values']}\n"
                f"ADB port(s): {signals['adb_ports']}\n\n"
                f"Host connectivity checks:\n{json.dumps(connectivity, indent=2)}\n"
                f"\nArtifact: {out_file}\n"
            )
            mail_result = try_send_mail(
                args.notify_email,
                f"Axiom job {args.job_id}: assignment detected",
                body,
            )
            event_data["mail_assignment"] = mail_result
            snapshot.update(event_data)
            out_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            write_report(report_file, snapshot)
            notified_assignment = True

        if has_adb and not notified_adb:
            body = (
                f"Job {args.job_id} ADB details detected.\n\n"
                f"Host(s): {signals['hosts']}\n"
                f"Serial(s): {signals['serial_values']}\n"
                f"ADB port(s): {signals['adb_ports']}\n"
                f"Keys scanned: {signals['adb_keys']}\n\n"
                f"Artifact: {out_file}\n"
            )
            mail_result = try_send_mail(
                args.notify_email,
                f"Axiom job {args.job_id}: ADB details detected",
                body,
            )
            event_data["mail_adb"] = mail_result
            snapshot.update(event_data)
            out_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            write_report(report_file, snapshot)
            notified_adb = True

        if time.time() - started >= args.max_wait:
            break
        if notified_assignment and notified_adb:
            break
        time.sleep(args.poll_interval)

    print(json.dumps(last_snapshot, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
