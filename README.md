# Axiom Public API Automation

CLI automation for Qualcomm Axiom Public API.

This project is designed for direct use by end users:
- Initializes ready-to-edit payload templates.
- Reads credentials from `.env` automatically.
- Submits a job, polls status, gets logs summary, and optionally creates a coverage report.
- Saves all responses to an output folder for debugging and traceability.

## What This Tool Does

`axiom_flow.py` can run the following in one command:
1. Optional permission refresh (`PUT /users/updatepermission`)
2. Submit a fresh job (`POST /jobs/submit`)
3. Poll job status (`GET /jobs/{id}/info`)
4. Fetch job results/log summary (`GET /jobs/{id}/results`)
5. Optional coverage report creation (`POST /coveragereport`)
6. Optional report instance creation (`POST /coveragereport/{id}/instances`)
7. Optional resource lookup (`GET /resources/{id}`)

## Prerequisites

- Python 3.9+
- Axiom service account with correct taxonomy permissions
- Access to `https://api-int.qualcomm.com`

## Quick Start (Plug and Play)

### 1. Create your env file

```bash
cp .env.example .env
```

Edit `.env` with your values:
- `AXIOM_CLIENT_ID`
- `AXIOM_CLIENT_SECRET`

Optional:
- `AXIOM_ACCESS_TOKEN` (if you already have a token)

Note:
- The script always requests a fresh token using client credentials on every run.
- Shell environment variables override values from `.env`.

### 2. Generate starter payloads

```bash
python3 axiom_flow.py --init-samples --out-dir ./tmp
```

This creates:
- `./tmp/job_payload.json`
- `./tmp/report_payload.json`

### 3. Edit payloads with your real values

At minimum, update these in `./tmp/job_payload.json`:
- `team`
- `metaBuild.path`
- `playlists[].id`
- `resource.identifier`

Update `./tmp/report_payload.json` only if you plan to create a coverage report.

### 4. Run job flow

```bash
python3 axiom_flow.py \
  --out-dir ./tmp \
  --job-mode Standard \
  --job-type Standard \
  --poll-interval 10 \
  --poll-timeout 900 \
  --resource-id 11
```

Because `./tmp/job_payload.json` exists, `--job-payload-file` is optional after initialization.

### 5. (Optional) Run with report creation

```bash
python3 axiom_flow.py \
  --out-dir ./tmp \
  --job-mode Standard \
  --job-type Standard \
  --job-payload-file ./tmp/job_payload.json \
  --report-payload-file ./tmp/report_payload.json \
  --create-report-instance
```

## Authentication

Fresh OAuth token is minted on every run using client credentials:
- CLI: `--client-id ... --client-secret ...`
- Env: `AXIOM_CLIENT_ID`, `AXIOM_CLIENT_SECRET`

## Common Commands

Initialize templates (safe, no overwrite):
```bash
python3 axiom_flow.py --init-samples --out-dir ./tmp
```

Initialize and overwrite existing templates:
```bash
python3 axiom_flow.py --init-samples --force-init-samples --out-dir ./tmp
```

Refresh permissions before main flow:
```bash
python3 axiom_flow.py --refresh-permissions --out-dir ./tmp
```

Use custom env file:
```bash
python3 axiom_flow.py --env-file ./my.env --out-dir ./tmp
```

## Output Files

Typical artifacts in `--out-dir`:
- `run_summary.json`
- `job_submit_payload.json`
- `job_submit_response.json`
- `job_info_poll_*.json`
- `job_info_final.json`
- `job_results_page_0.json`
- `job_logs_summary.json`
- `connectivity_evidence.json` (host/device/serial/adb-port evidence summary from payload + results)
- `coveragereport_create_response.json` (if report enabled)
- `coveragereport_instance_response.json` (if report instance enabled)
- `resource_by_id_response.json` (if `--resource-id` is provided)

## Troubleshooting

- `Missing --job-payload-file`:
  - Run `--init-samples`, edit `./tmp/job_payload.json`, rerun.

- Token/auth errors:
  - Verify `AXIOM_CLIENT_ID` and `AXIOM_CLIENT_SECRET`.
  - Ensure account is allowed to fetch tokens.

- Write endpoint failures (submit/report):
  - Confirm taxonomy `Execute` permission.
  - If permissions were recently updated, rerun with `--refresh-permissions`.

- Job/read not immediately visible after submit:
  - The script already handles transient post-submit visibility delay.

## Security

- Do not commit `.env`, tokens, or secrets.
- Keep outputs in `tmp/` (already ignored by git).

## Runbook

- No-failure submission workflow: `RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md`
- Assignment/host/serial watcher: `scripts/watch_job_assignment.py`

## Kernel Taxonomy (APSS/LinuxKernel)

Pre-validated configuration for `/APSS/LinuxKernel` jobs.

### Known-good device

| Field | Value |
|-------|-------|
| Serial | `N10RPW017` |
| Chipset | SM8850 (Kaanapali) |
| Storage | UFS |
| Form factor | MTP |
| Host | `krnltm-axiom-14` |
| Resource ID | 181840 |
| Heartbeat | Active (verified 2026-05-06) |

### jobType=Standard

```bash
python3 axiom_flow.py \
  --env-file .env \
  --job-payload-file ./tmp/job_payload.json \
  --job-mode Standard \
  --job-type Standard \
  --out-dir ./tmp
```

Payload essentials:

```json
{
  "team": "/APSS/LinuxKernel",
  "metaBuild": {
    "path": "\\\\<server>\\<share>\\<meta>",
    "storageType": "UFS",
    "storageLayout": "Auto",
    "productFlavor": "Auto",
    "binaryType": "Auto"
  },
  "playlistVersionMode": "Custom",
  "playlists": [{ "id": 251, "revision": 13, "iteration": 1 }],
  "resource": { "type": "Device", "identifier": "N10RPW017" }
}
```

### jobType=DevFarm — Public API limitation

The "Axiom Dev Farm Playlist" (`id=17155`) only has revision 0.
The public API's `/jobs/submit` rejects revision 0 regardless of `playlistVersionMode`,
returning `"Invalid playlist with id 17155 and revision 0"`.

**Workaround:** Submit DevFarm jobs via the Axiom UI.
The payload template is saved at `tmp/job_payload_kaanapali_01181_devfarm.json` for reference.

---

## Job Completion Daemon

A persistent background service that monitors all submitted jobs and forwards
completion emails automatically.  No per-job setup required.

### How it works

1. `axiom_flow.py` calls `register_job()` after every successful submit — the daemon
   starts automatically if it is not already running.
2. A background thread per job polls `GET /jobs/{id}/info` every 60 s until terminal state.
3. After terminal state, the Gmail INBOX (`anurag.pateriya@oss.qualcomm.com`) is watched
   for up to 10 min for the Axiom completion email.
4. The original email is forwarded to `apateriy@qti.qualcomm.com`.
   If the Axiom email does not arrive in time, a synthetic HTML summary is sent instead.
5. The job is marked `done` in the registry and never processed again.

### Service files

| File | Purpose |
|------|---------|
| `tmp/daemon_service.pid` | PID of the running service |
| `tmp/daemon_service.log` | Structured log |
| `tmp/daemon_jobs.json` | Job registry — all jobs and their status |
| `tmp/job_daemon_<ID>.latest.json` | Latest poll snapshot per job |
| `tmp/job_daemon_<ID>.final.json` | Final job info at terminal state |

### Manually register a job

Use this when a job was submitted outside `axiom_flow.py` (e.g. via the Axiom UI):

```bash
python3 scripts/job_completion_daemon.py \
  --register-job-id <JOB_ID> \
  --register-forward apateriy@qti.qualcomm.com \
  --register-notify  anurag.pateriya@oss.qualcomm.com \
  --env-file .env --out-dir ./tmp
```

The daemon starts automatically if it is not running.

### Manually start the service

```bash
setsid python3 scripts/job_completion_daemon.py \
  --env-file .env --out-dir ./tmp \
  --poll-interval 60 --poll-timeout 86400 --mail-wait 600 \
  >> tmp/daemon_service.log 2>&1 &
echo $! > tmp/daemon_service.pid
```

### Monitor and control

```bash
tail -f tmp/daemon_service.log          # live log
cat tmp/daemon_jobs.json                # registry status
kill $(cat tmp/daemon_service.pid)      # stop service
```

### Required .env keys

```
AXIOM_CLIENT_ID=...
AXIOM_CLIENT_SECRET=...
IMAP_USER=anurag.pateriya@oss.qualcomm.com
IMAP_PASS=<gmail-app-password>
SMTP_USER=anurag.pateriya@oss.qualcomm.com
SMTP_PASS=<gmail-app-password>
NOTIFY_EMAIL=anurag.pateriya@oss.qualcomm.com
FORWARD_EMAIL=apateriy@qti.qualcomm.com
WINDOWS_USER=apateriy
# WINDOWS_PASS is not stored — agent prompts at runtime
```

Gmail credentials are read from `~/.muttrc` (already configured in this workspace).
