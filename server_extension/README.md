# Club resource delivery extension

This is an additive patch for the currently deployed license-service adapter,
not a replacement checkout of the historical license project. Existing license
generation, signatures, module bundles, devices and personal purchases stay in
their original service. The bot uses a separate signed service API.

## Prepared state, 2026-10-27

Production is unchanged pending the owner's separate deployment approval.
The two prepared images are `nero-club-sheet-payment-bot-backend:club-vip-20261027`
and `clipart-license-service:club-vip-20261027`; original images have matching
`before-club-vip-20261027` tags. The bot image contains a patch applied to its
actual running sources, avoiding unrelated unpublished changes in this checkout.
The server image replaces only `http_server.py` and adds `club_vip.py`.

## Deployment after approval

1. Recheck the running image identities and the private backup in
   `/opt/bots-stack/backups/nero-club-vip-20261027T162915Z`. SQLite integrity passed;
   the full PostgreSQL archive restored successfully to disposable `club_restore`
   with 73 licenses and 8 device records. Configuration secrets are backed up
   privately, never printed.
2. Generate a dedicated random `CLUB_VIP_SERVICE_SECRET` (at least 32 characters)
   into a private `0600` env file outside the repository and load it in both
   services. Do not reuse an administrator browser session, password or token.
3. Apply `club_vip.sql` in one PostgreSQL transaction, then
   `preserve_historical_owner.sql`. The latter is mandatory for this deployment:
   the owner declined replacing her two lost historical codes. Null encrypted
   values deliberately produce an unavailable notice instead of duplicate keys.
4. Attach the bot and license service to a dedicated private Compose network.
   Set bot `CLUB_LICENSE_BASE_URL=http://license-service:8000` and
   `CLUB_RESOURCES_DRY_RUN=false`. Preserve existing `DRY_RUN` and all unrelated
   flags; this new setting enables only resource jobs, not removals/payments.
5. Add persistent Compose override files selecting the prepared images, shared
   private env file and network. Preserve every existing Compose override from
   the running containers' labels. Restart the license service, then bot with
   `up -d --no-deps --no-build`. The scheduler's endpoints are unchanged.
6. Check health and test authenticated service connectivity. Exercise actual
   handlers on an in-memory SQLite copy with a capture-only Telegram sender.
   Existing users with both licenses can verify adoption without generating
   new licenses. Never replay real credential delivery as a smoke test.
7. Run registry checks and compare all original license/device/module records
   with the backup. No old code, verifier, device or purchased right may change.
   On failure, restore the original bot image, then license image with Compose.
   Additive schema can remain during image rollback; do not delete newly issued
   licenses or restore old databases over real user activity automatically.

## Catalog and access

`club_vip_products` is the server-side list of eligible products. Initial rows
include the two current applications; Studio references the existing managed
`club-vip` module bundle. Adding an enabled product to this catalog makes it
available on the next eligible user request. Removing a catalog product stops
new issuance; it does not silently revoke previously issued licenses.

`club_vip_grants` uniquely binds Telegram ID and product. A transaction-level
advisory lock serializes requests and encrypted keys allow safe replay after a
lost response. Imported personal licenses retain `managed=false`; suspension
affects only new managed club licenses. Manual revocation is never restored.
Historical licenses without their plaintext code are withheld, never replaced.

The primary group is the membership authority. Channel events cannot revoke its
resource rights. Restricted users qualify only with `is_member=true`; local
deny/manual ban always blocks delivery. Billing and Telegram admission keep
their existing rules. Automatic membership jobs recheck the primary group
before restoring or suspending resources. Real app GUI, actual Telegram delivery
and browser login remain user scenarios, not covered by capture-only checks.

## Verification

`sh tools/verify.sh` passed locally (5 gate tests, 133 pytest). The prepared
production-derived bot image passed 119 tests without network. An additional
20-case handler test run covers ordinary and whitelisted unpaid members.
`tools/check_club_service.py` is executed only in the license image sharing a
disposable PostgreSQL network namespace, with `NERO_DISPOSABLE_CLUB_TEST=yes`.
It uses a fixed loopback `club_test` DSN and no production configuration.
It verifies signed HTTP, concurrent/repeated issuance, encrypted persistence,
module grants, suspension/restoration, personal-license preservation, manual
revocation, future products and rejection without duplicate generation.
