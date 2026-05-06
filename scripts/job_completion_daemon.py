#!/usr/bin/env python3
"""
Axiom Job Completion Daemon  (persistent, multi-job)
=====================================================
A single long-running process that:

  1. Watches a job registry file (tmp/daemon_jobs.json) for newly registered jobs.
  2. Spawns a background thread per active job that polls /jobs/{id}/info until
     a terminal state is reached.
  3. After terminal state, watches the Gmail INBOX for the Axiom completion email
     and forwards it (or a synthetic summary) to the configured forward address.
  4. Marks the job as done in the registry so it is never processed twice.

The daemon starts automatically the first time axiom_flow.py submits a job
(via register_job()) and keeps running indefinitely.  If it is already running,
register_job() just appends to the registry and the daemon picks it up.

Registry file: tmp/daemon_jobs.json
PID file:      tmp/daemon_service.pid
Log file:      tmp/daemon_service.log

Manual start:
    python3 scripts/job_completion_daemon.py --env-file .env --out-dir ./tmp

Register a job from any script:
    from scripts.job_completion_daemon import register_job
    register_job(job_id=12345, forward_to="user@qti.qualcomm.com")
"""

from __future__ import annotations

import argparse
import base64
import email as email_mod
import email.utils
import fcntl
import imaplib
import json
import logging
import os
import smtplib
import ssl
import sys
import threading
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ── constants ──────────────────────────────────────────────────────────────────
TERMINAL_STATES   = {"Completed", "Aborted", "Failed", "SetupFailed", "Cancelled"}
REGISTRY_FILENAME = "daemon_jobs.json"
PID_FILENAME      = "daemon_service.pid"
LOG_FILENAME      = "daemon_service.log"
REGISTRY_SCAN_SEC = 15          # how often the main loop checks for new jobs
TOKEN_REFRESH_SEC = 3000        # refresh OAuth token every ~50 min
MAIL_WAIT_SEC     = 600         # seconds to wait for Axiom email after completion
IMAP_POLL_SEC     = 30          # IMAP check interval while waiting for mail

LOG_FMT = "%(asctime)s [%(levelname)s] %(threadName)s | %(message)s"


# ── logging setup (file + stdout) ─────────────────────────────────────────────

def setup_logging(out_dir: Path, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    # Only add handlers once (guard against double-call)
    if not root.handlers:
        fmt = logging.Formatter(LOG_FMT)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        log_path = out_dir / LOG_FILENAME
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


log = logging.getLogger("axiom-daemon")


# ── .env loader ────────────────────────────────────────────────────────────────

def load_dotenv(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key   = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


# ── job registry ───────────────────────────────────────────────────────────────

def _registry_path(out_dir: Path) -> Path:
    return out_dir / REGISTRY_FILENAME


def _lock_registry(fh) -> None:
    fcntl.flock(fh, fcntl.LOCK_EX)


def _unlock_registry(fh) -> None:
    fcntl.flock(fh, fcntl.LOCK_UN)


def load_registry(out_dir: Path) -> List[Dict[str, Any]]:
    p = _registry_path(out_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_registry(out_dir: Path, jobs: List[Dict[str, Any]]) -> None:
    p = _registry_path(out_dir)
    p.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def register_job(
    job_id: int,
    forward_to: Optional[str] = None,
    notify_email: Optional[str] = None,
    out_dir: str = "./tmp",
    env_file: str = ".env",
    taxonomy: str = "/APSS/LinuxKernel",
    meta_build: str = "",
    submitter: str = "",
    auto_start_daemon: bool = True,
) -> None:
    """
    Register a job for monitoring.  Called by axiom_flow.py after every submit.
    If the daemon is not running, starts it automatically (auto_start_daemon=True).
    """
    load_dotenv(env_file)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    fwd  = forward_to   or os.environ.get("FORWARD_EMAIL", "apateriy@qti.qualcomm.com")
    ntfy = notify_email or os.environ.get("NOTIFY_EMAIL",  "anurag.pateriya@oss.qualcomm.com")

    reg_path = _registry_path(out)
    with open(reg_path, "a+") as fh:
        _lock_registry(fh)
        fh.seek(0)
        raw = fh.read().strip()
        jobs: List[Dict[str, Any]] = json.loads(raw) if raw else []

        # Avoid duplicates
        existing_ids = {j["job_id"] for j in jobs}
        if job_id not in existing_ids:
            jobs.append({
                "job_id":      job_id,
                "status":      "pending",   # pending | watching | done | error
                "forward_to":  fwd,
                "notify_email": ntfy,
                "taxonomy":    taxonomy,
                "meta_build":  meta_build,
                "submitter":   submitter,
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "terminal_state": None,
                "mail_forwarded": None,
                "completed_at": None,
            })
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(jobs, indent=2))
            print(f"[register_job] Job {job_id} registered → {reg_path}")
        else:
            print(f"[register_job] Job {job_id} already in registry – skipped.")
        _unlock_registry(fh)

    if auto_start_daemon:
        _ensure_daemon_running(out_dir, env_file)


def _ensure_daemon_running(out_dir: str, env_file: str) -> None:
    """Start the daemon as a detached process if it is not already running."""
    out = Path(out_dir)
    pid_file = out / PID_FILENAME
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)   # signal 0 = existence check
            return            # already running
        except (ProcessLookupError, ValueError):
            pass              # stale PID – fall through and restart

    script = Path(__file__).resolve()
    log_path = out / LOG_FILENAME
    cmd = (
        f"setsid python3 {script} --env-file {env_file} --out-dir {out_dir} "
        f">> {log_path} 2>&1 & echo $!"
    )
    pid_str = os.popen(cmd).read().strip()
    if pid_str.isdigit():
        pid_file.write_text(pid_str)
        print(f"[ensure_daemon] Daemon started with PID {pid_str}")
    else:
        print(f"[ensure_daemon] WARNING: could not start daemon (got: {pid_str!r})")


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def request_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    body: Optional[bytes] = None
    hdrs = dict(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = Request(url=url, method=method, headers=hdrs, data=body)
    try:
        with urlopen(req, timeout=120) as resp:
            code = resp.getcode()
            raw  = resp.read().decode("utf-8")
    except HTTPError as exc:
        code = exc.code
        raw  = exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return 599, {"error": str(exc)}
    try:
        return code, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return code, {"raw": raw}


def mint_token(apigee_url: str) -> str:
    cid  = os.environ.get("AXIOM_CLIENT_ID",     "").strip()
    csec = os.environ.get("AXIOM_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise RuntimeError("Missing AXIOM_CLIENT_ID / AXIOM_CLIENT_SECRET")
    encoded = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    url  = f"{apigee_url.rstrip('/')}/ent/oauth/v1/accesstoken?grant_type=client_credentials"
    code, data = request_json("POST", url, {"Authorization": f"Basic {encoded}"})
    if code >= 300:
        raise RuntimeError(f"Token request failed ({code}): {data}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {data}")
    return token


def api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization":    f"Bearer {token}",
        "X-QCOM-TracingID": uuid.uuid4().hex,
        "X-QCOM-AppName":   "AxiomPublicAPI",
        "X-QCOM-TokenType": "OAuth",
        "X-QCOM-ClientType": "automation-script",
    }


# ── per-job worker thread ──────────────────────────────────────────────────────

class JobWatcher(threading.Thread):
    """One thread per active job.  Polls until terminal, then handles email."""

    def __init__(
        self,
        job: Dict[str, Any],
        out_dir: Path,
        apigee_url: str,
        poll_interval: int,
        poll_timeout: int,
        mail_wait: int,
    ) -> None:
        super().__init__(
            name=f"job-{job['job_id']}",
            daemon=True,
        )
        self.job          = job
        self.job_id       = job["job_id"]
        self.forward_to   = job.get("forward_to",   "apateriy@qti.qualcomm.com")
        self.notify_email = job.get("notify_email", "anurag.pateriya@oss.qualcomm.com")
        self.out_dir      = out_dir
        self.apigee_url   = apigee_url
        self.poll_interval = poll_interval
        self.poll_timeout  = poll_timeout
        self.mail_wait     = mail_wait

        self.imap_user = os.environ.get("IMAP_USER", "anurag.pateriya@oss.qualcomm.com")
        self.imap_pass = os.environ.get("IMAP_PASS", "")
        self.smtp_user = os.environ.get("SMTP_USER", "anurag.pateriya@oss.qualcomm.com")
        self.smtp_pass = os.environ.get("SMTP_PASS", "")
        self.from_addr = os.environ.get("NOTIFY_EMAIL", "anurag.pateriya@oss.qualcomm.com")

    # ── registry helpers ──────────────────────────────────────────────────────

    def _update_registry(self, **kwargs) -> None:
        reg_path = _registry_path(self.out_dir)
        with open(reg_path, "r+") as fh:
            _lock_registry(fh)
            jobs = json.loads(fh.read())
            for j in jobs:
                if j["job_id"] == self.job_id:
                    j.update(kwargs)
                    break
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(jobs, indent=2))
            _unlock_registry(fh)

    # ── Axiom polling ─────────────────────────────────────────────────────────

    def _poll_to_terminal(self) -> Dict[str, Any]:
        base  = f"{self.apigee_url.rstrip('/')}/axiom/v1/public"
        token = mint_token(self.apigee_url)
        hdrs  = api_headers(token)
        minted_at = time.time()

        request_json("PUT", f"{base}/users/updatepermission", hdrs)
        self._update_registry(status="watching")

        deadline = time.time() + self.poll_timeout
        attempt  = 0

        while time.time() < deadline:
            if time.time() - minted_at > TOKEN_REFRESH_SEC:
                log.info("Refreshing OAuth token for job %d …", self.job_id)
                token     = mint_token(self.apigee_url)
                hdrs      = api_headers(token)
                minted_at = time.time()

            attempt += 1
            code, info = request_json("GET", f"{base}/jobs/{self.job_id}/info", hdrs)

            if code == 401:
                token     = mint_token(self.apigee_url)
                hdrs      = api_headers(token)
                minted_at = time.time()
                code, info = request_json("GET", f"{base}/jobs/{self.job_id}/info", hdrs)

            if code >= 300:
                log.warning("Job %d poll #%d: HTTP %d", self.job_id, attempt, code)
                time.sleep(self.poll_interval)
                continue

            state = info.get("state", "Unknown")
            log.info(
                "Job %d | poll #%d | state=%-14s | started=%s | ended=%s",
                self.job_id, attempt, state,
                info.get("started") or "—",
                info.get("ended")   or "—",
            )

            snap = self.out_dir / f"job_daemon_{self.job_id}.latest.json"
            snap.write_text(json.dumps(info, indent=2), encoding="utf-8")

            if state in TERMINAL_STATES:
                log.info("Job %d → terminal state: %s", self.job_id, state)
                return info

            time.sleep(self.poll_interval)

        log.warning("Job %d: poll timeout after %d s", self.job_id, self.poll_timeout)
        return {}

    # ── IMAP mail watcher ─────────────────────────────────────────────────────

    def _wait_for_axiom_mail(self) -> Optional[bytes]:
        log.info("Job %d: watching INBOX for Axiom mail (up to %d s) …",
                 self.job_id, self.mail_wait)
        deadline = time.time() + self.mail_wait
        seen_ids: set = set()

        while time.time() < deadline:
            try:
                conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                conn.login(self.imap_user, self.imap_pass)
                conn.select("INBOX")
                _, msgs = conn.search(None, "ALL")
                all_ids = set(msgs[0].split())
                new_ids = all_ids - seen_ids
                seen_ids = all_ids

                for mid in sorted(new_ids):
                    _, data = conn.fetch(mid, "(RFC822)")
                    raw = data[0][1]
                    msg = email_mod.message_from_bytes(raw)
                    subj = msg.get("Subject", "")
                    if self._subject_matches(subj):
                        log.info("Job %d: Axiom mail found → [%s]", self.job_id, subj)
                        conn.logout()
                        return raw
                conn.logout()
            except Exception as exc:
                log.warning("Job %d: IMAP error: %s", self.job_id, exc)

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(IMAP_POLL_SEC, remaining))

        log.warning("Job %d: Axiom mail not found within %d s", self.job_id, self.mail_wait)
        return None

    def _subject_matches(self, subject: str) -> bool:
        sl = subject.lower()
        has_id       = str(self.job_id) in subject
        has_terminal = any(t.lower() in sl for t in TERMINAL_STATES)
        has_axiom    = "axiom" in sl
        return has_id and (has_terminal or has_axiom)

    # ── email send / forward ──────────────────────────────────────────────────

    def _forward_raw(self, raw_bytes: bytes) -> bool:
        original = email_mod.message_from_bytes(raw_bytes)
        if "To" in original:
            original.replace_header("To", self.forward_to)
        else:
            original["To"] = self.forward_to
        original["X-Forwarded-To"]   = self.forward_to
        original["X-Forwarded-From"] = original.get("From", "")
        if "From" in original:
            original.replace_header("From", self.from_addr)
        else:
            original["From"] = self.from_addr
        return self._smtp_send_raw(original.as_bytes())

    def _send_synthetic(self, info: Dict[str, Any]) -> bool:
        state    = info.get("state", "Unknown")
        build    = info.get("build", "N/A")
        started  = info.get("started") or "N/A"
        ended    = info.get("ended")   or "N/A"
        product  = info.get("softwareProduct", "N/A")
        taxonomy = info.get("taxonomyPath", "N/A")
        subject  = f"Axiom Job {self.job_id} {state} – {product}"

        plain = (
            f"Axiom Job Completion Notification\n"
            f"==================================\n"
            f"Job ID    : {self.job_id}\n"
            f"State     : {state}\n"
            f"Product   : {product}\n"
            f"Build     : {build}\n"
            f"Taxonomy  : {taxonomy}\n"
            f"Started   : {started}\n"
            f"Ended     : {ended}\n\n"
            f"(Synthetic – original Axiom email not received in time.)\n"
            f"Axiom UI  : https://axiom.qualcomm.com/jobs/{self.job_id}\n"
        )
        color = "green" if state == "Completed" else "red"
        html = (
            f"<html><body>"
            f"<h2>Axiom Job Completion Notification</h2>"
            f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>"
            f"<tr><td><b>Job ID</b></td><td>{self.job_id}</td></tr>"
            f"<tr><td><b>State</b></td><td style='color:{color}'><b>{state}</b></td></tr>"
            f"<tr><td><b>Product</b></td><td>{product}</td></tr>"
            f"<tr><td><b>Build</b></td><td>{build}</td></tr>"
            f"<tr><td><b>Taxonomy</b></td><td>{taxonomy}</td></tr>"
            f"<tr><td><b>Started</b></td><td>{started}</td></tr>"
            f"<tr><td><b>Ended</b></td><td>{ended}</td></tr>"
            f"</table>"
            f"<p><i>Synthetic notification – original Axiom email not received in time.</i></p>"
            f"<p><a href='https://axiom.qualcomm.com/jobs/{self.job_id}'>View in Axiom UI</a></p>"
            f"</body></html>"
        )
        msg = MIMEMultipart("alternative")
        msg["From"]       = self.from_addr
        msg["To"]         = self.forward_to
        msg["Subject"]    = subject
        msg["Message-ID"] = email.utils.make_msgid()
        msg["Date"]       = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))
        return self._smtp_send_raw(msg.as_bytes())

    def _smtp_send_raw(self, raw: bytes) -> bool:
        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
                s.login(self.smtp_user, self.smtp_pass)
                s.sendmail(self.from_addr, [self.forward_to], raw)
            log.info("Job %d: email forwarded to %s ✓", self.job_id, self.forward_to)
            return True
        except Exception as exc:
            log.error("Job %d: SMTP error: %s", self.job_id, exc)
            return False

    # ── main thread body ──────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("JobWatcher started for job %d → forward to %s",
                 self.job_id, self.forward_to)
        try:
            final_info = self._poll_to_terminal()
            if not final_info:
                self._update_registry(status="error", terminal_state="timeout")
                return

            state = final_info.get("state", "Unknown")
            final_path = self.out_dir / f"job_daemon_{self.job_id}.final.json"
            final_path.write_text(json.dumps(final_info, indent=2), encoding="utf-8")

            raw_mail = self._wait_for_axiom_mail()
            if raw_mail:
                ok = self._forward_raw(raw_mail)
                mail_forwarded = "original" if ok else "failed"
            else:
                ok = self._send_synthetic(final_info)
                mail_forwarded = "synthetic" if ok else "failed"

            self._update_registry(
                status="done",
                terminal_state=state,
                mail_forwarded=mail_forwarded,
                completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            log.info("Job %d done. state=%s mail=%s", self.job_id, state, mail_forwarded)

        except Exception as exc:
            log.exception("Job %d: unhandled error: %s", self.job_id, exc)
            self._update_registry(status="error", terminal_state=str(exc))


# ── main service loop ──────────────────────────────────────────────────────────

def run_service(
    out_dir: Path,
    apigee_url: str,
    poll_interval: int,
    poll_timeout: int,
    mail_wait: int,
) -> None:
    """
    Persistent main loop.  Scans the registry every REGISTRY_SCAN_SEC seconds
    and spawns a JobWatcher thread for every job in 'pending' status.
    """
    active_jobs: set = set()   # job_ids currently being watched

    log.info("═" * 60)
    log.info("Axiom Job Completion Daemon  [persistent service]")
    log.info("  Registry : %s", _registry_path(out_dir))
    log.info("  PID      : %d", os.getpid())
    log.info("  Scan     : every %d s", REGISTRY_SCAN_SEC)
    log.info("═" * 60)

    # Write PID file
    (out_dir / PID_FILENAME).write_text(str(os.getpid()))

    while True:
        try:
            jobs = load_registry(out_dir)
            for job in jobs:
                jid    = job["job_id"]
                status = job.get("status", "pending")
                if status == "pending" and jid not in active_jobs:
                    watcher = JobWatcher(
                        job=job,
                        out_dir=out_dir,
                        apigee_url=apigee_url,
                        poll_interval=poll_interval,
                        poll_timeout=poll_timeout,
                        mail_wait=mail_wait,
                    )
                    watcher.start()
                    active_jobs.add(jid)
                    log.info("Spawned watcher thread for job %d", jid)

            # Prune finished jobs from active set
            active_jobs = {
                jid for jid in active_jobs
                if any(j["job_id"] == jid and j.get("status") not in ("done", "error")
                       for j in jobs)
            }

        except Exception as exc:
            log.warning("Registry scan error: %s", exc)

        time.sleep(REGISTRY_SCAN_SEC)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Axiom Job Completion Daemon – persistent multi-job service"
    )
    p.add_argument("--env-file",      default=".env")
    p.add_argument("--apigee-url",    default="https://api-int.qualcomm.com")
    p.add_argument("--out-dir",       default="./tmp")
    p.add_argument("--poll-interval", type=int, default=60,
                   help="Seconds between per-job Axiom API polls (default 60)")
    p.add_argument("--poll-timeout",  type=int, default=86400,
                   help="Max seconds to wait per job (default 24 h)")
    p.add_argument("--mail-wait",     type=int, default=MAIL_WAIT_SEC,
                   help="Seconds to wait for Axiom email after completion (default 600)")
    p.add_argument("--log-level",     default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    # Optional: register a single job and exit (used by axiom_flow.py integration)
    p.add_argument("--register-job-id",   type=int, default=None,
                   help="Register a job ID and exit (daemon auto-started if needed)")
    p.add_argument("--register-forward",  default=None,
                   help="Forward address for --register-job-id")
    p.add_argument("--register-notify",   default=None,
                   help="Notify address for --register-job-id")
    p.add_argument("--register-meta",     default="",
                   help="Meta build path for --register-job-id")
    p.add_argument("--register-submitter", default="",
                   help="Submitter username for --register-job-id")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir, args.log_level)

    # ── register-only mode (called by axiom_flow.py) ──────────────────────────
    if args.register_job_id is not None:
        register_job(
            job_id=args.register_job_id,
            forward_to=args.register_forward,
            notify_email=args.register_notify,
            out_dir=args.out_dir,
            env_file=args.env_file,
            meta_build=args.register_meta,
            submitter=args.register_submitter,
            auto_start_daemon=True,
        )
        return 0

    # ── service mode ─────────────────────────────────────────────────────────
    run_service(
        out_dir=out_dir,
        apigee_url=args.apigee_url,
        poll_interval=args.poll_interval,
        poll_timeout=args.poll_timeout,
        mail_wait=args.mail_wait,
    )
    return 0   # never reached


if __name__ == "__main__":
    raise SystemExit(main())
