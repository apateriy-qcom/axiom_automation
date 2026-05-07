---
name: axiom-public-api
description: Work with Qualcomm Axiom Public APIs for authentication, permission refresh, endpoint selection, and request construction. Use this skill when users ask to list/query Axiom entities (jobs, testcases, playlists, resources, pools, coverage plans, test plans, events), submit or abort jobs/events, generate coverage reports, or build curl/Python automation against Axiom Public API. Also covers job completion monitoring, Gmail-based email forwarding, and the persistent job_completion_daemon workflow.
---

# Axiom Public API

## Overview

Use this skill to execute the full Axiom Public API workflow: setup assumptions, OAuth token retrieval, mandatory header construction, permission propagation, safe request sequencing, and endpoint-specific request shaping.

## Execute Workflow

1. Validate prerequisites before any API call.
- Confirm service account is added to a QGroup with taxonomy access.
- Confirm taxonomy permission level:
  - `View` for read endpoints.
  - `Execute` for write endpoints (`POST` and `PUT`).

2. Get token with client credentials.
- Build `base64(client_id:client_secret)` using `scripts/generate_basic_auth.py`.
- Request token from:
  - `POST https://api-int.qualcomm.com/ent/oauth/v1/accesstoken?grant_type=client_credentials`
- Treat token TTL as 1 hour and refresh proactively for long runs.

3. Build request headers for every public API call.
- Always send:
  - `Authorization: Bearer <access_token>`
  - `X-QCOM-TracingID: <trace_id>`
  - `X-QCOM-AppName: <tool_or_app_name>`
  - `X-QCOM-TokenType: OAuth`
  - `X-QCOM-ClientType: <client_name>`
- Use unique tracing IDs per call chain for audit/debug correlation.

4. Refresh permissions before reads when identity/entitlements changed.
- Call `PUT /users/updatepermission` first after QGroup/taxonomy changes.
- Also call it when results appear stale or unexpectedly incomplete.

5. Select endpoint family and apply constraints.
- Use `references/endpoints.md` for endpoint catalog and high-value query patterns.
- Respect date window rules (many entities are limited to one month windows).
- For writes, wait about 5 seconds before dependent reads due to replication delay.

6. Handle job submission payloads carefully.
- Start from minimal valid payload and add optional fields incrementally.
- Enforce conditional requirements (`jobMode`, `jobType`, `playlistVersionMode`, resource type rules).
- For `localFile.content` values, convert using `scripts/convert_localfile_content.py`.

## Use References

- Read [`references/quickstart.md`](references/quickstart.md) for auth flow, headers, and guardrails.
- Read [`references/endpoints.md`](references/endpoints.md) for endpoint catalog and high-value query patterns.
- Read [`RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md`](RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md) for the full no-failure job submission workflow for APSS/LinuxKernel.
- Load only the sections relevant to the user task to keep context compact.

## Use Scripts

- `scripts/generate_basic_auth.py` — Generate Base64 string for `client_id:client_secret`.
- `scripts/convert_localfile_content.py` — Convert file content to JSON-escaped CRLF string.
- `scripts/watch_job_assignment.py` — Poll job assignment, test host connectivity, send assignment email.
- `scripts/job_completion_daemon.py` — Persistent multi-job completion daemon (see section below).

## Output Standards

- Prefer concrete curl snippets first, then Python if requested.
- Include exact required headers in every generated sample.
- Include explicit UTC timestamps in ISO 8601 (`YYYY-MM-DDTHH:mm:ssZ`) for date filters.
- Warn when request likely fails due to missing `Execute` permission on write endpoints.
- For polling/read-after-write flows, include explicit 5-second wait guidance.

## Kernel Taxonomy Job Submission

### Taxonomy and team
- Taxonomy: `/APSS/LinuxKernel`
- Team field in payload: `"/APSS/LinuxKernel"`

### jobType=Standard (kernel regression)
- Known-good playlist: `id=251`, `revision=13`, `iteration=1`, `playlistVersionMode=Custom`
- Device: `N10RPW017` (SM8850, UFS, MTP, host=`krnltm-axiom-14`, resource id=181840)
- storageType: `UFS`
- Submit: `POST /jobs/submit?jobMode=Standard&jobType=DevFarm` *(recommended — avoids CMS permission error)*

### jobType=DevFarm
- Use playlist `id=251`, `revision=13`, `iteration=1`, `playlistVersionMode=Custom`
  (same playlist as Standard — DevFarm skips CMS content sync, so no permission error)
- Device: `N10RPW017`, `buildLoading=None`, `storageType=UFS`
- Submit: `POST /jobs/submit?jobMode=Standard&jobType=DevFarm`
- **Do NOT use playlist 17155** ("Axiom Dev Farm Playlist") via public API —
  it only has revision 0 which the API rejects. Playlist 251 rev 13 works identically.
- Key difference vs Standard: DevFarm bypasses `//depot/MPSS/` CMS sync entirely,
  avoiding the `SystemError` that hits `kernelbaseport` with `jobType=Standard`.

### Meta build path format
- Pattern: `\\\\<server>\\<share>\\<product>.<branch>-<build_id>`
- Example: `\\\\crmhyd\\nsid-hyd-06\\Kaanapali.LA.1.0-01181-STD.TM-1`
- storageType is always `UFS` for this taxonomy.
- storageLayout, productFlavor, binaryType: always `Auto`.

### Minimal Standard payload template
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
  "resource": { "type": "Device", "identifier": "N10RPW017" },
  "optional": {
    "emailNotifications": {
      "recipients": ["apateriy@qti.qualcomm.com"],
      "schedule": { "jobStart": true, "jobEnd": true, "buildLoad": false, "resourceConfig": false }
    }
  }
}
```

## Job Completion Daemon  (persistent service)

`scripts/job_completion_daemon.py` is a **single always-on process** that monitors
all submitted jobs automatically.  It never needs to be started per-job.

### Architecture
- **One persistent process** scans `tmp/daemon_jobs.json` every 15 s.
- **One thread per active job** polls `/jobs/{id}/info` until terminal state.
- After terminal state, watches Gmail INBOX for the Axiom completion email (up to 10 min).
- Forwards the original email (or a synthetic HTML summary) to `FORWARD_EMAIL`.
- Marks the job `done` in the registry so it is never processed twice.
- OAuth token is refreshed automatically every 50 min per thread.

### Auto-start on first job submit
`axiom_flow.py` calls `register_job()` automatically after every successful
`/jobs/submit`.  `register_job()` checks if the daemon is running (via PID file)
and starts it with `setsid` if not.  No manual intervention needed.

### Manual registration (any job, any user)
```bash
python3 scripts/job_completion_daemon.py \
  --register-job-id <JOB_ID> \
  --register-forward <forward@email.com> \
  --register-notify  <notify@email.com> \
  --env-file .env --out-dir ./tmp
```
This registers the job and auto-starts the daemon if it is not running.

### Manual service start (if needed)
```bash
setsid python3 scripts/job_completion_daemon.py \
  --env-file .env --out-dir ./tmp \
  --poll-interval 60 --poll-timeout 86400 --mail-wait 600 \
  >> tmp/daemon_service.log 2>&1 &
echo $! > tmp/daemon_service.pid
```

### Service files
| File | Purpose |
|------|---------|
| `tmp/daemon_service.pid` | PID of the running service |
| `tmp/daemon_service.log` | Structured log (tail -f to watch) |
| `tmp/daemon_jobs.json` | Job registry (all jobs + status) |
| `tmp/job_daemon_<ID>.latest.json` | Latest poll snapshot per job |
| `tmp/job_daemon_<ID>.final.json` | Final job info at terminal state |

### Registry job status values
`pending` → `watching` → `done` | `error`

### .env keys used by daemon
| Key | Purpose |
|-----|---------|
| `AXIOM_CLIENT_ID` | OAuth client ID |
| `AXIOM_CLIENT_SECRET` | OAuth client secret |
| `IMAP_USER` | Gmail address to watch (default: `anurag.pateriya@oss.qualcomm.com`) |
| `IMAP_PASS` | Gmail app password |
| `SMTP_USER` | Gmail address to send from |
| `SMTP_PASS` | Gmail app password |
| `NOTIFY_EMAIL` | Primary recipient (Axiom sends job mails here) |
| `FORWARD_EMAIL` | Secondary address to forward completion mail to |
| `WINDOWS_USER` | Windows workspace username (default: `apateriy`) |
| `WINDOWS_PASS` | Windows password — **not stored in `.env`**, prompted at runtime |

### Gmail credentials
- IMAP/SMTP: `imap.gmail.com:993` / `smtp.gmail.com:465` (SSL)
- Account: `anurag.pateriya@oss.qualcomm.com`
- App password in `.env` as `IMAP_PASS` / `SMTP_PASS`
- mutt config at `~/.muttrc` — use to manually inspect inbox if needed

### Terminal job states
`Completed`, `Aborted`, `Failed`, `SetupFailed`, `Cancelled`

### Useful commands
```bash
tail -f tmp/daemon_service.log          # watch live
cat tmp/daemon_jobs.json                # check registry
kill $(cat tmp/daemon_service.pid)      # stop service
```
