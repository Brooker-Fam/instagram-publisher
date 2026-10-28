# Connect a professional Instagram account

The service starts in **demo mode**. Demo requests exercise the publishing workflow
without sending content to Instagram. Meta connection setup and public HTTPS hosting
are separate from building the repository.

## What you need

- A Meta developer account and an app configured for **Instagram API with Instagram Login**.
- Access to the professional Instagram account you want to publish to. Use a **Business** account for
  this project's feed, Reel, and Story workflow; API support can differ by account type.
- A stable HTTPS hostname for this service, for example `https://publisher.example.com`.
- The Instagram app ID and secret from the Meta app dashboard. Store the secret in the
  deployment's secret settings or private `.env`, never in source control.

This integration uses Instagram Login directly. It does not require the Facebook Login
route or assume that a Facebook Page is linked.

## 1. Create and configure the Meta app

1. Open [Meta for Developers](https://developers.facebook.com/apps/) and create an app
   with the Instagram use case. Meta's dashboard wording can change; choose the setup
   for **Instagram API with Instagram Login**.
2. Add these permissions to that setup:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
3. Set the valid OAuth redirect URI to exactly:

   ```text
   https://YOUR-PUBLISHER-HOST/auth/instagram/callback
   ```

   Match scheme, hostname, path, and trailing slash exactly. Use the callback that the
   running service displays if your deployment is configured differently.
4. Add the intended account/app role or Instagram test account using Meta's development
   setup, and accept any invitation in Instagram. The person completing login must be
   authorized to manage the intended Instagram account.
5. Configure these lifecycle callback URLs in the Meta app dashboard:

   ```text
   Deauthorization: https://YOUR-PUBLISHER-HOST/auth/instagram/deauthorize
   Data deletion:   https://YOUR-PUBLISHER-HOST/auth/instagram/data-deletion
   ```

   Both routes accept Meta's signed `POST` request. They verify the HMAC signature with
   the Instagram app secret; an unsigned browser request is not a substitute for Meta's
   callback. The deletion response provides an opaque confirmation code and a public
   status URL under `/auth/instagram/deletion-status/`.
6. Complete the app dashboard's requested privacy policy and business details before
   seeking production access. Those policies must describe your actual deployment and
   retention practices; this repository is not a published legal policy.

Meta documents **Standard Access** for professional accounts that you own or manage and
have added to the app. Serving accounts outside that setup requires **Advanced Access**,
and Meta may require App Review and business verification. Check the dashboard's current
requirements for this app rather than treating development access as approval to onboard
unrelated customers.

## 2. Configure the deployed service

Create the private environment file once:

```sh
python scripts/bootstrap-env.py
```

Keep the generated `ADMIN_API_KEY`, `TOKEN_ENCRYPTION_KEY`, and `MEDIA_SIGNING_KEY`.
Set the following values in the deployment configuration:

```dotenv
PUBLISHER_ENV=production
PUBLISHER_MODE=live
PUBLIC_BASE_URL=https://YOUR-PUBLISHER-HOST
META_APP_ID=YOUR_INSTAGRAM_APP_ID
META_APP_SECRET=YOUR_INSTAGRAM_APP_SECRET
META_API_VERSION=v26.0
```

The callback is `PUBLIC_BASE_URL` plus `/auth/instagram/callback`. The version is
configurable so Meta version upgrades can be tested deliberately.

Switching to `live` makes authorized publishing requests capable of posting to the
connected account. It does not grant an account connection or permission by itself.
Bring the service up, finish login, and review the selected account and drafts before
scheduling a real post. Do not carry unreviewed demo records into production; start
with a separate database and upload volume for the live deployment.

## 3. Complete Instagram authorization

1. Open the service's `/docs` page. Use **Authorize** to enter the admin API key as the
   Bearer credential. The page exposes interactive API documentation; it is not a
   separate social-media dashboard.
2. Execute `POST /api/accounts/connect`. The service generates a short-lived, single-use
   OAuth state and returns an `authorization_url`.
3. Sign in directly on Instagram's authorization page and grant the requested permissions.
4. Instagram redirects to the configured callback. The service exchanges the code for
   a token and stores the account connection. It encrypts the stored token with
   `TOKEN_ENCRYPTION_KEY`.
5. Call `GET /api/accounts` and check the returned Instagram username and account ID
   against the account you intend to use.

You never need to put your Instagram password into this service. Completing OAuth is
the account owner's action; building this repository cannot complete it automatically.

## 4. Check public media delivery

Meta retrieves the file from the media URL supplied during publishing. The service
issues an expiring signed URL for the uploaded file so Meta can retrieve that specific
asset without the administrator key.

- Use an HTTPS host reachable from the public internet.
- Allow the signed media route, OAuth callback, Meta lifecycle callbacks, and opaque
  deletion-status route through the reverse proxy.
- Do not put a blanket login screen, Cloudflare Access challenge, VPN requirement, or
  browser-only interstitial in front of those routes.
- Keep publishing, account-management, and upload endpoints protected by the service's
  administrator authentication.
- Treat signed URLs as temporary bearer links. Anyone who has one can retrieve that
  asset until it expires; do not put unrelated private files in uploads.

The supplied sample assets are for trying the demo workflow. For live publishing,
upload the files you have chosen for the connected account. A Reel needs an actual
video; its cover is only a still image.

## 5. Make one deliberate first post

Start with a reviewed single-image feed post and its final caption:

1. Upload the chosen image with `POST /api/assets`.
2. Create a draft with `POST /api/posts` using that asset and the intended account.
3. Review it with `GET /api/posts/{id}/review`. Check the account, preview, caption,
   alt text, and scheduled time.
4. Approve the exact reviewed version with `POST /api/posts/{id}/approve` and the
   current `revision` from the review. The running worker performs the queued job.
5. Read the finished record and verify the post on Instagram before adding a larger
   schedule. Keep demo and live results distinct.

Use the generated `/docs` schema for complete request bodies and available fields.
Editing an approved draft requires reviewing and approving the new revision.

Story graphics can be published as media, but this implementation does not add an
interactive link or Question sticker. Add those elements in Instagram using the manual
workflow when the post calls for them. Changing an Instagram profile picture is also
outside this project's publishing API.

## Tokens and reconnecting

Meta's long-lived Instagram tokens normally last 60 days. The token-refresh endpoint
accepts unexpired long-lived tokens that are at least 24 hours old. Before claiming
posts, the live worker checks for tokens that expire within seven days and were last
issued/refreshed more than 24 hours and one minute ago. It attempts to refresh each
eligible account at most once an hour, encrypting the replacement token on success.
Keep the worker running and monitor refresh failures and expiry; automatic refresh
still depends on Meta accepting the existing authorization.

The authenticated `POST /api/accounts/{id}/refresh` endpoint remains available for
manual refresh. Reconnect if permission is revoked or the token has expired. Never log
raw tokens, authorization codes, or app secrets while diagnosing a connection.

## Disconnecting and deleting account data

Meta's verified deauthorization callback removes the stored account token and cancels
queued posts. Its data-deletion callback removes the account and its linked post and
audit history. The implementation stores Meta's app-scoped user ID separately from the
professional Instagram publishing ID so callbacks apply to the intended connection.
Stale callback replays are rejected from removing a newer, reconnected account.

Uploaded images and videos belong to the publisher administrator and are retained
separately from the Meta account data. The operator must manage those uploads and
backups under the deployment's retention policy. A callback does not delete posts
already published on Instagram or promise erasure from independent backups.

The confirmation-status URL returns only the deletion request's status; it does not
expose the username, tokens, or account history. Keep that route reachable without
the publisher admin key, as required for Meta's data-deletion workflow.

## Official references

- [Instagram Login setup and token lifecycle](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/)
- [Get started and access levels](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started/)
- [Content publishing requirements](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/)
- [Media creation reference and supported formats](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/)
- [Meta's official Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
- [Instagram app configuration and lifecycle callbacks](https://developers.facebook.com/docs/instagram-platform/create-an-instagram-app/)
- [Meta data-deletion callback requirements](https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback/)

Meta changes permissions, supported formats, limits, and review requirements. The
service checks publishing quota with Meta rather than promising a fixed posts-per-day
allowance. Verify the current requirements when creating the Meta app and upgrading
the API version.
