#!/usr/bin/env python3
"""Run common Axiom Public API workflows from one CLI command."""

from __future__ import annotations

import argparse
import base64
import shutil
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Auto-register submitted jobs with the completion daemon
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from job_completion_daemon import register_job as _register_job
except Exception:
    _register_job = None

# Optional meta-based device selection (scripts/select_device.py)
try:
    import sys as _sys2
    _sys2.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
    from select_device import (
        product_from_meta as _product_from_meta,
        recent_successful_serials as _recent_successful_serials,
        index_devices_by_serial as _index_devices_by_serial,
        resolve_required_chipset as _resolve_required_chipset,
        rank_devices as _rank_devices,
    )
except Exception:
    _product_from_meta = None


TERMINAL_JOB_STATES = {
    "Completed",
    "Aborted",
    "Failed",
    "SetupFailed",
    "Cancelled",
}


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
    parser = argparse.ArgumentParser(
        description=(
            "Create fresh Axiom job, poll status, fetch logs, create report, and get resource by ID."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to env file. Variables already in shell take precedence.",
    )
    parser.add_argument("--apigee-url", default="https://api-int.qualcomm.com")
    parser.add_argument("--access-token", help="Deprecated: ignored. Fresh token is always minted.")
    parser.add_argument("--client-id", help="OAuth client ID (always used to mint fresh token)")
    parser.add_argument(
        "--client-secret", help="OAuth client secret (always used to mint fresh token)"
    )
    parser.add_argument(
        "--trace-id", default=uuid.uuid4().hex, help="Tracing ID header value"
    )
    parser.add_argument("--app-name", default="AxiomPublicAPI")
    parser.add_argument("--client-type", default="automation-script")
    parser.add_argument(
        "--out-dir", default="./tmp", help="Directory for request/response artifacts"
    )

    parser.add_argument(
        "--refresh-permissions",
        action="store_true",
        help="Call /users/updatepermission before other operations",
    )

    parser.add_argument(
        "--job-payload-file",
        help="Path to fresh /jobs/submit payload JSON",
    )
    parser.add_argument(
        "--init-samples",
        action="store_true",
        help="Copy sample payload files to output directory and exit",
    )
    parser.add_argument(
        "--force-init-samples",
        action="store_true",
        help="Overwrite existing payload files when used with --init-samples",
    )
    parser.add_argument("--job-mode", default="Standard")
    parser.add_argument("--job-type", default="Standard")
    parser.add_argument(
        "--auto-select-device",
        action="store_true",
        help="Pick a live, chipset-matching device for the meta and override the payload's "
        "resource block before submit (uses scripts/select_device.py).",
    )
    parser.add_argument(
        "--device-lookback-days",
        type=int,
        default=7,
        help="With --auto-select-device: how far back to scan jobs to learn the meta's chipset.",
    )
    parser.add_argument(
        "--heartbeat-max-age",
        type=int,
        default=1800,
        help="With --auto-select-device: max device heartbeat age in seconds (default 1800).",
    )
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--poll-timeout", type=int, default=900)
    parser.add_argument(
        "--post-submit-wait",
        type=int,
        default=5,
        help="Seconds to wait after submit before first status poll",
    )
    parser.add_argument(
        "--poll-not-found-grace",
        type=int,
        default=60,
        help="Treat /jobs/{id}/info 404 as transient for this many seconds",
    )
    parser.add_argument("--results-page-size", type=int, default=100)

    parser.add_argument(
        "--report-payload-file",
        help="Path to fresh /coveragereport payload JSON",
    )
    parser.add_argument(
        "--create-report-instance",
        action="store_true",
        help="After creating report, call POST /coveragereport/{reportId}/instances",
    )

    parser.add_argument("--resource-id", type=int, help="Resource ID for /resources/{id}")

    return parser.parse_args()


def read_json(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def init_sample_payloads(out_dir: Path, overwrite: bool) -> Dict[str, str]:
    repo_root = Path(__file__).resolve().parent
    samples = {
        "job": (
            repo_root / "templates" / "job_payload.sample.json",
            out_dir / "job_payload.json",
        ),
        "report": (
            repo_root / "templates" / "report_payload.sample.json",
            out_dir / "report_payload.json",
        ),
    }

    result: Dict[str, str] = {}
    for label, (src, dst) in samples.items():
        if not src.exists():
            raise FileNotFoundError(f"Missing sample file: {src}")
        if dst.exists() and not overwrite:
            result[label] = f"kept existing: {dst}"
            continue
        shutil.copyfile(src, dst)
        result[label] = f"written: {dst}"
    return result


def request_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    body: Optional[bytes] = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}

    req = Request(url=url, method=method, headers=headers, data=body)
    try:
        with urlopen(req, timeout=120) as resp:
            code = resp.getcode()
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8", errors="replace")
    except URLError as e:
        raise RuntimeError(f"Network error for {url}: {e}") from e

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    return code, parsed


def ensure_token(args: argparse.Namespace) -> str:
    env_client_id = os.environ.get("AXIOM_CLIENT_ID", "").strip()
    env_client_secret = os.environ.get("AXIOM_CLIENT_SECRET", "").strip()

    client_id = args.client_id or env_client_id
    client_secret = args.client_secret or env_client_secret

    if not (client_id and client_secret):
        raise ValueError(
            "Missing client credentials. Provide --client-id/--client-secret or AXIOM_CLIENT_ID+AXIOM_CLIENT_SECRET env vars."
        )

    encoded = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("utf-8")

    token_url = (
        f"{args.apigee_url.rstrip('/')}/ent/oauth/v1/accesstoken?grant_type=client_credentials"
    )
    headers = {"Authorization": f"Basic {encoded}"}
    code, data = request_json("POST", token_url, headers)
    if code >= 300:
        raise RuntimeError(f"Token request failed ({code}): {json.dumps(data)}")

    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in token response: {json.dumps(data)}")
    return token


def common_headers(args: argparse.Namespace, token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-QCOM-TracingID": args.trace_id,
        "X-QCOM-AppName": args.app_name,
        "X-QCOM-TokenType": "OAuth",
        "X-QCOM-ClientType": args.client_type,
    }


def auto_select_device(
    args: argparse.Namespace, base_url: str, headers: Dict[str, str], payload: Dict[str, Any]
) -> None:
    """Override payload['resource'] with a live, chipset-matching device for the meta.

    Raises RuntimeError (aborting submit) if no suitable device is found — surfacing
    the problem is the point; we never silently fall back to the payload's resource.
    """
    if _product_from_meta is None:
        raise RuntimeError("--auto-select-device requires scripts/select_device.py (import failed)")

    taxonomy = payload.get("team")
    meta_path = (payload.get("metaBuild") or {}).get("path", "")
    product = _product_from_meta(meta_path)
    if not taxonomy or not product:
        raise RuntimeError(
            f"--auto-select-device could not derive taxonomy/product (taxonomy={taxonomy}, product={product})"
        )

    serials = _recent_successful_serials(base_url, headers, taxonomy, product, args.device_lookback_days)
    device_index = _index_devices_by_serial(base_url, headers, taxonomy)
    required_chipset = _resolve_required_chipset(serials, device_index)
    if not required_chipset:
        raise RuntimeError(
            f"--auto-select-device could not resolve a chipset for '{product}': no recent "
            f"successful job in the last {args.device_lookback_days} day(s). "
            f"Increase --device-lookback-days or set resource manually."
        )

    eligible, rejected = _rank_devices(device_index, required_chipset, args.heartbeat_max_age, serials)
    if not eligible:
        same_chip = [r for r in rejected if r.get("chipset") == required_chipset]
        detail = "; ".join(f"{r['serial']}: {r['reason']}" for r in same_chip[:6]) or "none of that chipset"
        raise RuntimeError(
            f"--auto-select-device found chipset {required_chipset} for '{product}' but no live "
            f"device available. Rejected: {detail}"
        )

    chosen = eligible[0]["serial"]
    payload["resource"] = {"type": "Device", "identifier": chosen}
    print(
        f"[auto-select] product={product} chipset={required_chipset} "
        f"-> resource={{'type':'Device','identifier':'{chosen}'}} "
        f"(host={eligible[0]['hostname']}, hb_age={eligible[0]['heartbeat_age_s']}s)"
    )


def create_job(
    args: argparse.Namespace, base_url: str, headers: Dict[str, str], out_dir: Path
) -> int:
    payload = read_json(args.job_payload_file)
    if args.auto_select_device:
        auto_select_device(args, base_url, headers, payload)
    write_json(out_dir / "job_submit_payload.json", payload)

    qs = urlencode({"jobMode": args.job_mode, "jobType": args.job_type})
    url = f"{base_url}/jobs/submit?{qs}"
    code, resp = request_json("POST", url, headers, payload)
    write_json(out_dir / "job_submit_response.json", resp)
    if code >= 300:
        raise RuntimeError(f"Job submit failed ({code}): {json.dumps(resp)}")

    job_id: Optional[int] = None
    if isinstance(resp, list) and resp:
        job_id = resp[0].get("jobId")
    elif isinstance(resp, dict):
        job_id = resp.get("jobId")

    if not job_id:
        raise RuntimeError(f"Unable to parse jobId from submit response: {json.dumps(resp)}")
    job_id = int(job_id)
    # Auto-register with the persistent completion daemon
    if _register_job is not None:
        try:
            meta = payload.get("metaBuild", {}).get("path", "")
            _register_job(
                job_id=job_id,
                out_dir=str(out_dir),
                meta_build=meta,
                auto_start_daemon=True,
            )
        except Exception as _e:
            print(f"[daemon] register_job warning: {_e}")
    return job_id


def poll_job(
    job_id: int,
    args: argparse.Namespace,
    base_url: str,
    headers: Dict[str, str],
    out_dir: Path,
) -> Dict[str, Any]:
    start = time.time()
    attempt = 0
    not_found_since: Optional[float] = None
    while True:
        attempt += 1
        url = f"{base_url}/jobs/{job_id}/info"
        code, resp = request_json("GET", url, headers)
        write_json(out_dir / f"job_info_poll_{attempt}.json", {"httpCode": code, "data": resp})
        if code == 404:
            now = time.time()
            if not_found_since is None:
                not_found_since = now
            elapsed_404 = now - not_found_since
            if elapsed_404 <= args.poll_not_found_grace:
                print(f"[poll] jobId={job_id} not visible yet (404), retrying...")
                time.sleep(args.poll_interval)
                continue
            raise RuntimeError(
                f"Job info kept returning 404 beyond grace period: {json.dumps(resp)}"
            )
        if code >= 300:
            raise RuntimeError(f"Job info failed ({code}): {json.dumps(resp)}")
        not_found_since = None

        state = (resp or {}).get("state")
        print(f"[poll] jobId={job_id} state={state}")
        if state in TERMINAL_JOB_STATES:
            write_json(out_dir / "job_info_final.json", resp)
            return resp

        if time.time() - start >= args.poll_timeout:
            write_json(out_dir / "job_info_final.json", resp)
            return resp
        time.sleep(args.poll_interval)


def fetch_results_logs(
    job_id: int,
    args: argparse.Namespace,
    base_url: str,
    headers: Dict[str, str],
    out_dir: Path,
) -> Dict[str, Any]:
    url = (
        f"{base_url}/jobs/{job_id}/results?"
        + urlencode({"pageNumber": 0, "pageSize": args.results_page_size})
    )
    code, resp = request_json("GET", url, headers)
    write_json(out_dir / "job_results_page_0.json", resp)
    if code >= 300:
        raise RuntimeError(f"Job results failed ({code}): {json.dumps(resp)}")

    rows = (resp or {}).get("data") or []
    log_rows = []
    for row in rows:
        log_path = row.get("testCaseLogPath")
        log_link = row.get("testCaseOpenLogsDownloadLink")
        if log_path or log_link:
            log_rows.append(
                {
                    "executionId": row.get("executionId"),
                    "testCaseName": row.get("testCaseName"),
                    "result": row.get("testCaseTestResult"),
                    "testCaseLogPath": log_path,
                    "testCaseOpenLogsDownloadLink": log_link,
                }
            )

    summary = {
        "jobId": job_id,
        "results_count": len(rows),
        "rows_with_logs": len(log_rows),
        "log_rows": log_rows,
    }
    write_json(out_dir / "job_logs_summary.json", summary)
    return summary


def create_report(
    payload_file: str,
    base_url: str,
    headers: Dict[str, str],
    out_dir: Path,
    create_instance: bool,
) -> Dict[str, Any]:
    payload = read_json(payload_file)
    write_json(out_dir / "coveragereport_create_payload.json", payload)

    url = f"{base_url}/coveragereport"
    code, resp = request_json("POST", url, headers, payload)
    write_json(out_dir / "coveragereport_create_response.json", resp)
    if code >= 300:
        raise RuntimeError(f"Coverage report create failed ({code}): {json.dumps(resp)}")

    report_id = (resp or {}).get("reportId")
    out = {"create": resp, "instance": None}

    if create_instance and report_id:
        time.sleep(5)
        instance_url = f"{base_url}/coveragereport/{report_id}/instances"
        i_code, i_resp = request_json("POST", instance_url, headers)
        write_json(out_dir / "coveragereport_instance_response.json", i_resp)
        if i_code >= 300:
            raise RuntimeError(
                f"Coverage report instance create failed ({i_code}): {json.dumps(i_resp)}"
            )
        out["instance"] = i_resp
    return out


def get_resource(
    resource_id: int,
    base_url: str,
    headers: Dict[str, str],
    out_dir: Path,
) -> Dict[str, Any]:
    url = f"{base_url}/resources/{resource_id}"
    code, resp = request_json("GET", url, headers)
    write_json(out_dir / "resource_by_id_response.json", resp)
    if code >= 300:
        raise RuntimeError(f"Get resource failed ({code}): {json.dumps(resp)}")
    return resp


def build_connectivity_evidence(
    job_payload_file: str,
    final_info: Dict[str, Any],
    results_page: Dict[str, Any],
) -> Dict[str, Any]:
    payload = read_json(job_payload_file)
    rows = (results_page or {}).get("data") or []

    hostnames = sorted(
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
    result_values = [str(row.get("testCaseTestResult")) for row in rows if row.get("testCaseTestResult")]
    passed_count = sum(1 for r in result_values if r.lower() in {"pass", "passed", "success"})

    serial_keys = sorted(
        {
            key
            for row in rows
            for key in row.keys()
            if "serial" in key.lower()
        }
    )
    serial_values = sorted(
        {
            str(row.get(key))
            for row in rows
            for key in serial_keys
            if row.get(key)
        }
    )
    requested_serial = (payload.get("resource") or {}).get("identifier")
    if requested_serial and requested_serial not in serial_values:
        serial_values = [requested_serial, *serial_values]

    adb_related_keys = sorted(
        {
            key
            for row in rows
            for key in row.keys()
            if ("adb" in key.lower()) or ("port" in key.lower())
        }
    )
    adb_ports = set()
    pattern = re.compile(r"(?i)(?:adb[^0-9]{0,20}(\d{4,5})|(\d{4,5})[^0-9]{0,20}adb)")
    for row in rows:
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if "adb" not in value.lower():
                continue
            for match in pattern.finditer(value):
                port = match.group(1) or match.group(2)
                if port:
                    adb_ports.add(port)

    evidence = {
        "device_request": {
            "requested_via_jobs_submit": True,
            "team": payload.get("team"),
            "metaBuild": payload.get("metaBuild"),
            "playlistVersionMode": payload.get("playlistVersionMode"),
            "playlists": payload.get("playlists"),
            "resource": payload.get("resource"),
        },
        "job_progress": {
            "jobId": final_info.get("jobId"),
            "state": final_info.get("state"),
            "jobSetupState": final_info.get("jobSetupState"),
            "submitted": final_info.get("submitted"),
            "started": final_info.get("started"),
            "ended": final_info.get("ended"),
        },
        "assigned_windows_host_access": {
            "observed_hostnames": hostnames,
            "observed_in_results_count": len(hostnames),
            "checked_from_fields": ["playlistHostName", "testCaseHostName"],
        },
        "target_device_access_on_host": {
            "observed_test_resources": test_resources,
            "results_count": len(rows),
            "passed_testcase_count": passed_count,
            "checked_from_fields": ["playlistTestResource", "testCaseTestResourceName", "testCaseTestResult"],
        },
        "serial_access": {
            "candidate_serials": serial_values,
            "checked_from_fields": ["resource.identifier", *serial_keys],
        },
        "adb_port_access": {
            "candidate_adb_ports": sorted(adb_ports),
            "checked_from_fields": adb_related_keys,
            "note": "ADB port is inferred from result metadata strings only; testcase logs may contain additional details.",
        },
    }
    return evidence


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.init_samples:
        copied = init_sample_payloads(out_dir, args.force_init_samples)
        print(json.dumps({"initialized": copied, "out_dir": str(out_dir)}, indent=2))
        return 0

    if not args.job_payload_file:
        default_job_payload = out_dir / "job_payload.json"
        if default_job_payload.exists():
            args.job_payload_file = str(default_job_payload)
        else:
            raise ValueError(
                "Missing --job-payload-file. Run with --init-samples first, then edit "
                f"{default_job_payload} and rerun."
            )

    token = ensure_token(args)
    base_url = f"{args.apigee_url.rstrip('/')}/axiom/v1/public"
    headers = common_headers(args, token)

    summary: Dict[str, Any] = {
        "job": None,
        "job_final_state": None,
        "logs": None,
        "report": None,
        "resource": None,
        "artifacts_dir": str(out_dir),
    }

    if args.refresh_permissions:
        code, resp = request_json("PUT", f"{base_url}/users/updatepermission", headers)
        write_json(out_dir / "users_updatepermission_response.json", resp)
        if code >= 300:
            raise RuntimeError(
                f"Permission refresh failed ({code}): {json.dumps(resp)}"
            )

    job_id = create_job(args, base_url, headers, out_dir)
    summary["job"] = {"jobId": job_id}

    if args.post_submit_wait > 0:
        time.sleep(args.post_submit_wait)

    final_info = poll_job(job_id, args, base_url, headers, out_dir)
    summary["job_final_state"] = final_info.get("state")

    logs = fetch_results_logs(job_id, args, base_url, headers, out_dir)
    summary["logs"] = logs
    results_page = read_json(str(out_dir / "job_results_page_0.json"))
    # Use the effective submitted payload (reflects --auto-select-device override).
    effective_payload_file = str(out_dir / "job_submit_payload.json")
    evidence = build_connectivity_evidence(effective_payload_file, final_info, results_page)
    write_json(out_dir / "connectivity_evidence.json", evidence)
    summary["connectivity_evidence"] = evidence

    if args.report_payload_file:
        summary["report"] = create_report(
            args.report_payload_file,
            base_url,
            headers,
            out_dir,
            args.create_report_instance,
        )

    if args.resource_id is not None:
        resource = get_resource(args.resource_id, base_url, headers, out_dir)
        summary["resource"] = {
            "id": resource.get("id"),
            "type": resource.get("type"),
            "taxonomyPath": resource.get("taxonomyPath"),
            "isQuarantined": resource.get("isQuarantined"),
            "alias": resource.get("alias"),
        }

    write_json(out_dir / "run_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
