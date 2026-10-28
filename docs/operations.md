# Operate Maple Baby Publisher

## Publishing expectations

Draft creation is separate from publishing. Review the target account, image/video,
caption, alt text, and scheduled time before queuing a live post. Use an explicit
timezone when choosing a local date/time; store and compare scheduling times in UTC.

The API and worker use durable database records. The worker prepares Meta media
containers, waits for them to become ready, and submits the final publish request.
An uploaded file or a created container alone is not proof that a post was published.
The finished record should contain a Meta media ID; verify the corresponding result
on Instagram for the first production posts.

Meta containers expire after roughly 24 hours. Schedule the publisher's job for the
intended posting time; do not create containers days in advance and assume they will
remain publishable. The implementation requests Meta's current publishing-limit
status instead of assuming one permanent daily allowance.

## Token maintenance

Before claiming a job, the live worker checks unexpired account tokens that are within
seven days of expiry and were last issued/refreshed more than 24 hours and one minute
ago. It attempts a refresh at most once per account per hour, including after failures.
Successful replacements are encrypted before storage; a concurrent reconnect is not
overwritten by an older refresh response. Demo mode does not refresh Meta credentials.

Keep the worker running even when the queue is empty if you rely on this maintenance.
Monitor failed refresh events and token expiry. Use the authenticated
`POST /api/accounts/{id}/refresh` endpoint for a deliberate manual refresh, or complete
OAuth again when the token has expired or permission was revoked. A running worker
cannot restore authorization that Meta has withdrawn.

## Failed and uncertain attempts

Network timeouts can happen after Meta has accepted a publish request. Retrying that
request blindly can create an unwanted duplicate. The service does not automatically
replay an ambiguous publishing request.

When a job reports an uncertain or needs-attention state:

1. Inspect its safe error, recorded container/media identifiers, and timestamps.
2. Check the connected Instagram account for the expected post.
3. Call the authenticated `GET /api/posts/{id}/remote-status` endpoint to query the
   existing container. This read does not create a container or publish a post.
4. When Meta reports that container as `PUBLISHED`, call
   `POST /api/posts/{id}/reconcile`. It can mark a `NEEDS_ATTENTION` record as
   `PUBLISHED` only after Meta confirms that state; it never resubmits publication.
5. If Meta cannot confirm publication, retain the uncertain record for investigation.
   Do not clone and immediately resubmit a draft just because a response was lost.

An explicit Meta validation or permission rejection is different from a lost response.
For a job that failed before publication, correct the media/account/permission problem
and use `POST /api/posts/{id}/retry`. That resets an eligible job to a draft, requiring
a fresh review and revision approval. It does not blindly resubmit an uncertain publish
attempt. Preserve the existing record and its audit history when doing so.

Read `GET /api/events` for audit records and `GET /api/posts` for current job state.
`POST /api/posts/{id}/cancel` is for cancellable pending work; it cannot recall a post
that Instagram has already accepted.

## Process restarts and downtime

Keep one worker for the provided SQLite deployment. The database guards work claims,
but this starter is not a distributed high-availability scheduler. A long outage can
delay scheduled work; check overdue jobs after recovery before resuming a large queue.

Compose allows a 90-second graceful shutdown. The worker checks for shutdown around
provider calls and interrupts processing waits so it can stop without beginning a new
publish request. Do not replace graceful stops with immediate forced termination during
normal deployments. A host crash or forced kill can still leave an uncertain attempt;
the lease-recovery and reconciliation workflow exists for that case.

The initial schema is created at startup, with no versioned migration system yet.
Back up the database and inspect schema changes before upgrading. Test upgrades on a
separate copy with demo mode enabled before starting a worker on production data.

After an upgrade or restart:

- Confirm API health and worker startup.
- Confirm the configured mode is the intended `demo` or `live` mode.
- Inspect failed, pending, and uncertain attempts.
- Check account token expiry and any reconnect warning.
- Verify a small, reviewed job before adding further production work.

Do not scale the worker across hosts sharing a network-mounted SQLite file. A move to
multiple workers or replicas needs an intentional database/queue migration and load
testing; changing a connection URL alone does not provide that design.

## Back up and restore

Preserve both the SQLite database and uploaded media. Also preserve
`TOKEN_ENCRYPTION_KEY`, `MEDIA_SIGNING_KEY`, and the other deployment secrets in a
separate protected backup. A database backup without its encryption key cannot
restore the Instagram connection.

For a small deployment, stop both API and worker, snapshot/copy the whole `/data`
volume, and then restart. This avoids racing uploads, database writes, or a publish
attempt. Use your host's volume backup mechanism, encrypt backups, and periodically
test a restore into a separate demo environment. If taking a live SQLite backup,
use SQLite's backup API rather than copying only the database file while WAL writes
are in progress.

A restored old database may lack posts that went live after the snapshot. Before
resuming its queue, reconcile against Instagram to avoid repeating those posts.
Never run a restored live worker automatically as part of a backup test.

## Media and retention

Uploaded assets are served to Meta through signed, expiring URLs. Do not share those
URLs as permanent public links. Avoid deleting or moving files that pending jobs or
Meta's media-processing step still need. Keep completed-job records long enough to
investigate delivery and prevent accidental duplicate submissions.

The verified Meta deauthorization callback removes stored account credentials and
cancels queued posts. The verified data-deletion callback removes the account and
its linked post and audit history. Its public confirmation route exposes status only.
Stale callbacks cannot delete a newer reconnection. These callbacks do not retract
content already published on Instagram.

Administrator-uploaded files are retained separately from Meta account data. There
is no automatic business-specific retention policy for those files or independent
backups in this starter. Choose a policy for published media, unused uploads, and
backups before opening the service to anyone beyond the owner. Revoke the integration
through Instagram/Meta and apply the deployment's complete retention/deletion procedure
when retiring the service.

## Logging and alerts

Collect application and worker logs, but keep Uvicorn HTTP access logging disabled
with `--no-access-log`. Do not log authorization headers, raw OAuth responses,
Meta access tokens, app secrets, or complete signed media URLs. Leave reverse-proxy
access logs disabled or configure explicit query-string redaction: default request-URL
logging can retain OAuth codes/state and signed-media credentials. Do not paste private
`.env` contents into an issue or support message.

Useful operational signals include worker liveness, failed/uncertain jobs, jobs past
their intended publish time, disk usage, and token expiry. The health check confirms
the API responds; it does not by itself confirm the worker is running or Instagram
is accepting posts.

## Changing credentials

- **Admin API key:** replace it in both processes' configuration and in authorized
  clients; restart the services.
- **Meta app secret:** update the secret setting after rotating it in Meta and follow
  any reconnect requirements Meta reports.
- **Token encryption key:** do not replace it casually. Existing tokens need a
  controlled re-encryption procedure or account reconnection.
- **Media signing key:** rotating it invalidates existing signed links. Avoid doing
  so while Meta is retrieving files for a pending attempt.

For a suspected token or app-secret exposure, revoke/rotate the credential at Meta,
update the stored configuration, reconnect as required, and inspect recent activity.
