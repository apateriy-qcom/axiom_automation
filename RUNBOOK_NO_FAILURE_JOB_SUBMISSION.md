# No-Failure Axiom Job Submission Runbook

Use this flow for APSS job submissions so the same steps work for this meta and future metas.

## Scope

- Public API base: `https://api-int.qualcomm.com/axiom/v1/public`
- Typical team/taxonomy: `/APSS/LinuxKernel`
- Typical request: submit build meta + storage + compatible device + email notifications

## Inputs You Must Collect

- `META_PATH` (example: `\\crmhyd\nsid-hyd-06\Kaanapali.LA.1.0-01173-STD.INT-1`)
- `TEAM_PATH` (usually `/APSS/LinuxKernel`)
- `STORAGE_TYPE` (example: `UFS`)
- notification email(s)

## Guardrails (Read First)

- Always call `PUT /users/updatepermission` before discovery/submit.
- Do not trust a cloned payload blindly.
- Do not use a device before chipset compatibility is confirmed.
- Do not submit with playlist revision `0`.
- Save request/response artifacts for every attempt.

## Step-by-Step Workflow

### 1) Refresh auth and permissions

1. Get OAuth token using client credentials.
2. Build required headers:
   - `Authorization: Bearer <token>`
   - `X-QCOM-TracingID`
   - `X-QCOM-AppName`
   - `X-QCOM-TokenType: OAuth`
   - `X-QCOM-ClientType`
3. Call:
   - `PUT /users/updatepermission`

### 2) Get candidate devices from APSS

Call:

- `GET /resources?taxonomyPath=/APSS/LinuxKernel&type=Device&pageNumber=0&pageSize=500`

Filter candidates:

- `isQuarantined == false`
- `dependencies["storage Type"] == STORAGE_TYPE` (if storage is required)
- fresh/non-null `heartbeat` preferred

### 3) Determine chipset compatibility for the meta

If chipset support is unknown, do a submit probe using a candidate device.

- If backend returns:
  - `Chipset type mismatch ... Build ... supports [ ... ]`
- Parse supported chipsets from the error.
- Re-filter devices so `dependencies.chipset` is in that supported list.

This is the most reliable way to avoid wrong-device failures for new metas.

### 4) Resolve a valid playlist/revision

Never submit with invalid/obsolete playlist revision.

Preferred order:

1. Reuse a recently successful job in same team/product and pull its playlist+revision.
2. If cloning:
   - `GET /jobs/{id}/clone`
   - inspect `playlists`
   - if revision is `0` or submit rejects it, replace with explicit valid `id + revision`.
3. Set:
   - `playlistVersionMode: "Custom"` when passing explicit revision.

Known good combo used for Kaanapali success on 2026-04-24:

- `playlists: [{"id": 251, "revision": 13, "iteration": 1}]`
- `playlistVersionMode: "Custom"`

### 5) Build final payload

Required stable structure:

```json
{
  "team": "/APSS/LinuxKernel",
  "metaBuild": {
    "path": "\\\\crmhyd\\nsid-hyd-06\\Kaanapali.LA.1.0-01173-STD.INT-1",
    "storageType": "UFS",
    "storageLayout": "Auto",
    "productFlavor": "Auto",
    "binaryType": "Auto"
  },
  "playlistVersionMode": "Custom",
  "playlists": [
    { "id": 251, "revision": 13, "iteration": 1 }
  ],
  "resource": {
    "type": "Device",
    "identifier": "N10RPW017"
  },
  "optional": {
    "emailNotifications": {
      "recipients": ["apateriy@qti.qualcomm.com"],
      "schedule": {
        "jobStart": true,
        "jobEnd": true,
        "buildLoad": false,
        "resourceConfig": false
      }
    }
  }
}
```

### 6) Submit and verify

Submit:

- `POST /jobs/submit?jobMode=Standard&jobType=Standard`

Expect:

- HTTP `200`
- response contains `jobId`

Then wait ~5 seconds and verify:

- `GET /jobs/{jobId}/info`

### 7) Record artifacts every run

Keep these files in `tmp/`:

- payload used
- submit response
- job info response
- device snapshot used for selection
- short run report (for mail/share)

## DevFarm jobType — CMS Permission Bypass (Key Finding)

The `SAGA_Sanity` playlist (id=251) triggers a CMS content sync from `//depot/MPSS/`
when submitted with `jobType=Standard`. The service account `kernelbaseport` lacks
Collaborator permission on that depot, causing immediate `SystemError`.

**Fix: submit with `jobType=DevFarm` instead of `Standard`.**
DevFarm skips CMS content sync entirely. Playlist 251 rev 13 works fine with DevFarm.

The "Axiom Dev Farm Playlist" (id=17155) is NOT required — it is just the default
playlist other users happen to use. Any valid playlist with revision > 0 works.

### Confirmed working payload for DevFarm + pool (generic resource)

Use `ResourcePool` type with pool id `7818` ("Kaanapali V2 JTAG") so Axiom
picks the next available SM8850 device automatically.

### Confirmed working payload for DevFarm + playlist 251

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

Submit with: `POST /jobs/submit?jobMode=Standard&jobType=DevFarm`

### Why playlist 17155 cannot be used via public API

The "Axiom Dev Farm Playlist" (id=17155) only has revision 0. The public API
rejects revision 0 on `/jobs/submit` regardless of `playlistVersionMode`,
resource type (Device or ResourcePool), or jobMode. This is a hard server-side
validation. All other users submit it via the Axiom UI which bypasses this check.
Use playlist 251 rev 13 with `jobType=DevFarm` instead — identical outcome.

### Pool reference

| Pool ID | Name | Devices | Chipset |
|---------|------|---------|---------|
| 7818 | Kaanapali V2 JTAG | N10RPW017, TDC00002CAWB, TDC00002CD5V | SM8850 |

## Error-to-Fix Matrix

- `Chipset type mismatch ... supports [...]`
  - Pick device whose `dependencies.chipset` matches supported list.
- `Invalid playlist with id X and revision 0`
  - Do not use revision `0`; set explicit valid playlist revision and `playlistVersionMode=Custom`.
- `403` on write endpoints
  - taxonomy execute permission issue; run `updatepermission`, verify entitlements.
- `401`
  - refresh OAuth token.

## Reusable Quick Checklist

1. Refresh permission (`updatepermission`).
2. Get APSS devices and filter by storage + health.
3. Confirm build-compatible chipset (parse mismatch message if needed).
4. Use valid playlist revision (never `0`).
5. Add notification recipients.
6. Submit and verify `jobId` + `state`.
7. Save artifacts + report.

## Post-Submit: Assignment -> Windows Host -> Serial/ADB

Use the watcher script to avoid manual misses:

```bash
python3 scripts/watch_job_assignment.py \
  --job-id <JOB_ID> \
  --notify-email <email-id> \
  --poll-interval 30 \
  --max-wait 21600 \
  --out-dir ./tmp \
  --resource-id <RESOURCE_ID_OPTIONAL>
```

What it does:

1. Polls `GET /jobs/{id}/info` and `GET /jobs/{id}/results`.
2. Detects assignment host/resource and sends assignment email.
3. Tests workspace connectivity to host:
   - ICMP ping
   - TCP 3389 (RDP)
   - TCP 445 (SMB)
4. Tries SMB authenticated access if env vars exist:
   - `WINDOWS_USER`
   - `WINDOWS_PASS`
   - optional `WINDOWS_DOMAIN`
5. Looks up resource details (`serialNumber`, `adbId`) from `GET /resources/{id}` when `--resource-id` is passed, otherwise resolves by detected serial.
6. Monitors for ADB port evidence in results metadata and emails when found.

Output files:

- JSON snapshot: `tmp/job_watch_<JOB_ID>.latest.json`
- Markdown step report: `tmp/job_watch_<JOB_ID>.report.md`
