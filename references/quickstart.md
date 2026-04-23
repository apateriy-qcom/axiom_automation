# Axiom Public API Quickstart

## Base URLs

- Apigee host: `https://api-int.qualcomm.com`
- Public API base: `https://api-int.qualcomm.com/axiom/v1/public`
- Swagger UI: `https://publicapi-axiom.qualcomm.com/public/swagger-ui/index.html`

## Required Setup

1. Use a service account.
2. Add service account to a QGroup.
3. Ensure QGroup is in target taxonomy:
- `View` for read APIs.
- `Execute` for write APIs (`POST` / `PUT`).
4. Request `clientId` and `clientSecret` via Axiom support process.

## Permission Refresh Rule

Call `PUT /users/updatepermission` before other endpoints when:
- QGroup membership changed.
- Taxonomy permissions changed.
- API data appears stale or unexpectedly restricted.

## OAuth Flow

1. Build `base64(client_id:client_secret)`.
2. Request token:

```bash
curl --location --request POST \
  'https://api-int.qualcomm.com/ent/oauth/v1/accesstoken?grant_type=client_credentials' \
  --header 'Authorization: Basic <encoded_credentials>'
```

3. Use returned token as bearer token (token TTL: ~1 hour).

## Mandatory Headers For Public API

- `Authorization: Bearer <access_token>`
- `X-QCOM-TracingID: <trace_id>`
- `X-QCOM-AppName: <app_name>`
- `X-QCOM-TokenType: OAuth`
- `X-QCOM-ClientType: <client_type>`

## Universal Guardrails

- Use UTC timestamps in ISO 8601 format (`YYYY-MM-DDTHH:mm:ssZ`).
- Many list endpoints enforce one-month max query windows.
- Jobs/event retrieval has stricter time constraints in practice.
- After `POST`, wait ~5 seconds before `GET` due to write/read replication delay.
- Include pagination explicitly (`pageNumber`, `pageSize`) for deterministic automation.

## Frequent Error Causes

- Missing taxonomy permission (`403`).
- Skipping `/users/updatepermission` after entitlement changes.
- Invalid or expired bearer token (`401`).
- Invalid date range window (`400`).
- Missing conditionally required payload fields on `/jobs/submit` (`400`).
