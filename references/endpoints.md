# Axiom Public API Endpoint Map

Use this map to select the right endpoint family quickly.

## Taxonomy

- `GET /taxonomy`
- `GET /taxonomy/default`

Use to discover accessible taxonomy paths before filtering other calls.

## Jobs

- Query/list:
  - `GET /jobs`
  - `GET /jobs/{id}/info`
  - `GET /jobs/{id}/results`
  - `GET /jobs/{id}/results/{resultId}/metricSummary`
  - `GET /jobs/{id}/playlists`
  - `GET /jobs/{id}/testcases`
  - `GET /jobs/{id}/issues`
  - `GET /jobs/{id}/crashes`
  - `GET /jobs/{id}/iterationMetricSummary`
  - `GET /jobs/{id}/configuration`
  - `GET /jobs/{id}/build`
  - `GET /jobs/{id}/data/playlists`
  - `GET /jobs/{id}/data/playlists/{playlist_id}/iteration/{playlist_iteration}/results`
- Write:
  - `POST /jobs/submit`
  - `POST /jobs/abort`
- Utility:
  - `GET /jobs/{id}/clone`

### `/jobs/submit` high-risk conditionals

- Query params:
  - `jobMode` required: `Standard`, `TBS`, `TbsGuard`
  - `jobType` required: `Standard`, `Coverage`
- Payload:
  - `team`, `playlistVersionMode`, `playlists[]`, `resource` required.
  - `metaBuild` required unless job mode-specific override permits otherwise.
  - `coveragePlan` required when `jobType=Coverage`.
  - If `playlistVersionMode=Custom`, each playlist must include `revision`.
- Resource:
  - `resource.type`: `Device | VirtualDevice | TestEquipment | ResourceSet | ResourcePool`

## Testcases

- `GET /testcases`
- `GET /testcases/{id}/revisions`
- `GET /testcases/{id}/revisions/{revision}`

Typical use: fetch testcase metadata and historic revision snapshots.

## Playlists

- `GET /playlists`
- `GET /playlists/{id}/revisions`
- `GET /playlists/{id}/revisions/{revision}`
- `GET /playlists/{id}/revisions/latest`

Typical use: resolve latest or specific playlist revision before job submission.

## Coverage Plans

- `GET /coverageplans`
- `GET /coverageplans/{id}/revisions`
- `GET /coverageplans/{id}/revisions/{revisionId}/testcases`

Useful for deriving scope and testcase coverage details before coverage jobs.

## Test Plans

- `GET /testplans`
- `GET /testplans/{id}/revisions`
- `GET /testplans/{id}/testcases`

## Resources / Resource Sets / Pools

- Resources:
  - `GET /resources`
  - `GET /resources/{id}`
  - `PUT /resources/{id}/quarantine`
  - `PUT /resources/{id}/unquarantine`
- Resource sets:
  - `GET /resourcesets`
  - `GET /resourcesets/{id}`
- Pools:
  - `GET /pools`
  - `GET /pools/{id}/resources`
  - `GET /pools/{id}/resourcesets`
  - `PUT /pools/{id}/resourcesets/{resourcesetId}/isActiveInPool`

Write calls require `Execute` permission on relevant taxonomy.

## Users

- `PUT /users/updatepermission`

Call after entitlement changes and before data retrieval.

## Coverage Reports

- `GET /coveragereport`
- `POST /coveragereport`
- `GET /coveragereport/{reportId}/configuration`
- `GET /coveragereport/{reportId}/instances`
- `POST /coveragereport/{reportId}/instances`
- `GET /coveragereport/{reportId}/instances/{instanceId}`
- `GET /coveragereport/{reportId}/instances/{instanceId}/issues`
- `GET /coveragereport/{reportId}/instances/{instanceId}/requirements`
- `GET /coveragereport/{reportId}/instances/{instanceId}/results`
- `GET /coveragereport/{reportId}/instances/{instanceId}/testcases`

Constraints documented in source content include:
- per-day request limits
- active/concurrent instance limits
- replication delay after POST

## Events

- `GET /events`
- `POST /events`
- `POST /events/abort`
- `GET /events/{id}`

Use to trigger and inspect event-driven job flows.

## Post Processing

- `POST /taskrun/testcase/start`
- `POST /taskrun/testcase/end`
- `POST /taskrun`
- `GET /taskrun/{id}/status`

Use for external testcase lifecycle integration and task-run automation.

## Triggers

- `GET /triggers/{id}/events`

Use to list event history attached to a trigger ID.
