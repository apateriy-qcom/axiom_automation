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
- If `AXIOM_ACCESS_TOKEN` is not set, the script will request a token using client credentials.
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

## Authentication Options

Choose one:

1. Access token
- CLI: `--access-token ...`
- Env: `AXIOM_ACCESS_TOKEN`

2. Client credentials
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
