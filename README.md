# Axiom Public API Automation

CLI automation for the Qualcomm **Axiom Public API** (`https://api-int.qualcomm.com/axiom/v1/public`).

Submit a test job, poll it to a terminal state, pull the results/log summary, and
capture host/device/serial/ADB assignment evidence — all from one command, with every
request and response saved to an output folder for traceability.

This README documents only the **verified working path**. Everything here has been run
end-to-end against the live API.

---

## Repo layout

```
axiom_flow.py                     # main CLI: submit -> poll -> results -> connectivity evidence
scripts/
  watch_job_assignment.py         # poll a job for assigned host/serial/ADB, test connectivity, email
  job_completion_daemon.py        # persistent daemon: forward Axiom completion emails per job
  generate_basic_auth.py          # base64(client_id:client_secret) helper
  convert_localfile_content.py    # JSON-escape a file's content for localFile.content payloads
templates/
  job_payload.kaanapali_kernel.json   # proven /APSS/LinuxKernel DevFarm payload
  job_payload.sample.json             # generic starter payload (edit for your team)
  report_payload.sample.json          # coverage-report starter payload
references/
  quickstart.md                   # auth flow, headers, guardrails
  endpoints.md                    # endpoint catalog + query patterns
RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md  # step-by-step submission runbook + error matrix
SKILL.md                          # skill manifest / kernel taxonomy quick reference
legacy/                           # old BAIT wrapper (axiom_launch_and_monitor.py) — not maintained
```

---

## Prerequisites

- Python 3.9+ (standard library only — no `pip install` needed)
- An Axiom service account with the right taxonomy permission:
  - `View` for read endpoints (GET)
  - `Execute` for write endpoints (POST/PUT — e.g. job submit)
- Network access to `https://api-int.qualcomm.com`

---

## 1. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
AXIOM_CLIENT_ID=your_client_id
AXIOM_CLIENT_SECRET=your_client_secret
```

A fresh OAuth token is minted from these on **every** run (TTL ~1 hour). Shell
environment variables override `.env`. The full set of optional keys (email/daemon
config) is documented in [Job completion daemon](#job-completion-daemon).

---

## 2. Submit a job

`axiom_flow.py` reads a payload JSON, submits it, polls to a terminal state, fetches the
results summary, and writes a connectivity-evidence file. All artifacts land in `--out-dir`.

### Quick start (generic)

```bash
# Create starter payloads in ./tmp
python3 axiom_flow.py --init-samples --out-dir ./tmp

# Edit ./tmp/job_payload.json: team, metaBuild.path, playlists[].id, resource.identifier

python3 axiom_flow.py \
  --env-file .env \
  --job-payload-file ./tmp/job_payload.json \
  --job-mode Standard \
  --job-type Standard \
  --poll-interval 15 \
  --poll-timeout 900 \
  --out-dir ./tmp
```

### Kernel (Kaanapali / `/APSS/LinuxKernel`) — proven path

Use `jobType=DevFarm` with `ResourcePool 7818` ("Kaanapali V2 JTAG", 3× SM8850
devices). DevFarm skips the CMS content sync from `//depot/MPSS/`, which avoids the
`SystemError` that `jobType=Standard` hits for `kernelbaseport`.

```bash
python3 axiom_flow.py \
  --env-file .env \
  --job-payload-file ./templates/job_payload.kaanapali_kernel.json \
  --job-mode Standard \
  --job-type DevFarm \
  --poll-interval 15 \
  --poll-timeout 180 \
  --out-dir ./tmp
```

Proven payload (`templates/job_payload.kaanapali_kernel.json`):

```json
{
  "team": "/APSS/LinuxKernel",
  "metaBuild": {
    "path": "\\\\crmhyd\\nsid-hyd-06\\Kaanapali.LA.1.0-01181-STD.TM-1",
    "storageType": "UFS",
    "storageLayout": "Auto",
    "productFlavor": "Auto",
    "binaryType": "Auto"
  },
  "playlistVersionMode": "LastPublished",
  "playlists": [{ "id": 17155, "iteration": 1 }],
  "resource": { "type": "ResourcePool", "identifier": "7818" },
  "optional": {
    "buildLoading": "None",
    "emailNotifications": {
      "recipients": ["apateriy@qti.qualcomm.com"],
      "schedule": { "jobStart": true, "jobEnd": true, "buildLoad": false, "resourceConfig": false }
    }
  }
}
```

**Playlist notes (verified 2026-06-23):**

- Playlist **17155** ("Axiom Dev Farm Playlist") **works** via the public API. It only
  has revision 0, so submit with `playlistVersionMode=LastPublished` (or `UnpublishedTip`)
  and **omit the `revision` field**. Passing an explicit `"revision": 0` is what the server
  rejects with `400 Invalid playlist with id 17155 and revision 0`.
- Playlist **251 rev 13** also works (`playlistVersionMode=Custom`,
  `playlists:[{"id":251,"revision":13,"iteration":1}]`) and is a drop-in alternative.
- Never submit `revision: 0` explicitly, and `playlistVersionMode=Latest` is not a valid value.

### Pool / device reference

| Pool ID | Name | Devices | Chipset |
|---------|------|---------|---------|
| 7818 | Kaanapali V2 JTAG | `N10RPW017`, `TDC00002CAWB`, `TDC00002CD5V` | SM8850 |

To target one device directly instead of the pool, use
`"resource": { "type": "Device", "identifier": "N10RPW017" }`.

---

## 3. Get the assigned host / serial / ADB

A freshly submitted job sits in `Submitted` with no host yet. Watch it until Axiom assigns
a device, then capture host + serial + ADB and test workspace connectivity:

```bash
python3 scripts/watch_job_assignment.py \
  --job-id <JOB_ID> \
  --notify-email apateriy@qti.qualcomm.com \
  --poll-interval 30 \
  --max-wait 21600 \
  --out-dir ./tmp
```

It polls `/jobs/{id}/info` + `/jobs/{id}/results`, extracts the assigned host/resource,
runs ping / RDP-3389 / SMB-445 checks (and authenticated SMB if `WINDOWS_USER`/`WINDOWS_PASS`
are set), looks up `serialNumber`/`adbId` via `/resources/{id}` (pass `--resource-id`), and
emails on assignment / ADB detection. Outputs:

- `tmp/job_watch_<JOB_ID>.latest.json` — JSON snapshot
- `tmp/job_watch_<JOB_ID>.report.md` — Markdown step report

---

## Output artifacts (`--out-dir`)

| File | Purpose |
|------|---------|
| `run_summary.json` | Top-level run summary |
| `job_submit_payload.json` / `job_submit_response.json` | Exact submit request/response |
| `job_info_poll_*.json` / `job_info_final.json` | Status polls + final state |
| `job_results_page_0.json` / `job_logs_summary.json` | Results page + log-link summary |
| `connectivity_evidence.json` | Host / device / serial / ADB evidence derived from payload + results |
| `coveragereport_*` | Coverage report responses (only with `--report-payload-file`) |
| `resource_by_id_response.json` | Resource lookup (only with `--resource-id`) |

---

## Optional flows

Refresh taxonomy permissions before submitting (run after QGroup/entitlement changes):

```bash
python3 axiom_flow.py --refresh-permissions --job-payload-file ./tmp/job_payload.json --out-dir ./tmp
```

Create a coverage report after the job:

```bash
python3 axiom_flow.py \
  --job-payload-file ./tmp/job_payload.json \
  --report-payload-file ./tmp/report_payload.json \
  --create-report-instance \
  --out-dir ./tmp
```

Look up a resource by ID (enriches the run with serial/host/quarantine state):

```bash
python3 axiom_flow.py --job-payload-file ./tmp/job_payload.json --resource-id 181840 --out-dir ./tmp
```

---

## Job completion daemon

`scripts/job_completion_daemon.py` is a single persistent process that monitors all
submitted jobs and forwards their Axiom completion emails. `axiom_flow.py` auto-registers
each submitted job and starts the daemon if it is not already running — no per-job setup.

Manually register a job submitted elsewhere (e.g. the Axiom UI):

```bash
python3 scripts/job_completion_daemon.py \
  --register-job-id <JOB_ID> \
  --register-forward apateriy@qti.qualcomm.com \
  --register-notify  anurag.pateriya@oss.qualcomm.com \
  --env-file .env --out-dir ./tmp
```

Control / inspect:

```bash
tail -f tmp/daemon_service.log      # live log
cat tmp/daemon_jobs.json            # job registry + status (pending -> watching -> done|error)
kill $(cat tmp/daemon_service.pid)  # stop the service
```

`.env` keys used by the daemon:

| Key | Purpose |
|-----|---------|
| `AXIOM_CLIENT_ID` / `AXIOM_CLIENT_SECRET` | OAuth credentials |
| `IMAP_USER` / `IMAP_PASS` | Gmail account to watch (IMAP `imap.gmail.com:993`) |
| `SMTP_USER` / `SMTP_PASS` | Gmail account to send from (SMTP `smtp.gmail.com:465`) |
| `NOTIFY_EMAIL` | Address Axiom sends job mail to |
| `FORWARD_EMAIL` | Address to forward completion mail to |
| `WINDOWS_USER` | Windows host user for SMB/RDP checks (`WINDOWS_PASS` is prompted at runtime, never stored) |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Missing --job-payload-file` | Run `--init-samples`, edit `./tmp/job_payload.json`, rerun |
| `401` | Token expired/invalid — credentials are re-minted each run; verify `AXIOM_CLIENT_ID`/`SECRET` |
| `403` on submit | Missing `Execute` permission on the taxonomy; run `--refresh-permissions` |
| `400 Invalid playlist ... revision 0` | Don't pass explicit `revision: 0`; use `LastPublished` and omit `revision` |
| `400 Chipset type mismatch ... supports [...]` | Pick a device/pool whose chipset is in the supported list |
| `SystemError` for `kernelbaseport` with `jobType=Standard` | Submit kernel jobs with `jobType=DevFarm` |
| Job `/info` 404 right after submit | Transient replication delay — `axiom_flow.py` already retries within a grace window |

See [`RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md`](RUNBOOK_NO_FAILURE_JOB_SUBMISSION.md) for the
full submission workflow and [`references/`](references/) for the auth flow and endpoint catalog.

---

## Security

- `.env`, `tmp/`, and `__pycache__/` are git-ignored. Never commit credentials or tokens.
- `WINDOWS_PASS` is intentionally never written to `.env` — it is prompted at runtime.
