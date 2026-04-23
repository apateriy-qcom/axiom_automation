---
name: axiom-public-api
description: Work with Qualcomm Axiom Public APIs for authentication, permission refresh, endpoint selection, and request construction. Use this skill when users ask to list/query Axiom entities (jobs, testcases, playlists, resources, pools, coverage plans, test plans, events), submit or abort jobs/events, generate coverage reports, or build curl/Python automation against Axiom Public API.
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
- Use `references/endpoints.md` for endpoint selection and common filters.
- Respect date window rules (many entities are limited to one month windows).
- For writes, wait about 5 seconds before dependent reads due to replication delay.

6. Handle job submission payloads carefully.
- Start from minimal valid payload and add optional fields incrementally.
- Enforce conditional requirements (`jobMode`, `jobType`, `playlistVersionMode`, resource type rules).
- For `localFile.content` values, convert using `scripts/convert_localfile_content.py`.

## Use References

- Read [`references/quickstart.md`](references/quickstart.md) for auth flow, headers, and guardrails.
- Read [`references/endpoints.md`](references/endpoints.md) for endpoint catalog and high-value query patterns.
- Load only the sections relevant to the user task to keep context compact.

## Use Scripts

- `scripts/generate_basic_auth.py`
  - Generate Base64 string for `client_id:client_secret`.
- `scripts/convert_localfile_content.py`
  - Convert file content to JSON-escaped CRLF string for `localFile.content`.

## Output Standards

- Prefer concrete curl snippets first, then Python if requested.
- Include exact required headers in every generated sample.
- Include explicit UTC timestamps in ISO 8601 (`YYYY-MM-DDTHH:mm:ssZ`) for date filters.
- Warn when request likely fails due to missing `Execute` permission on write endpoints.
- For polling/read-after-write flows, include explicit 5-second wait guidance.
