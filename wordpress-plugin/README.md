# Nero Club Access WordPress plugin

The plugin exposes a narrow, HMAC-authenticated endpoint for the self-hosted backend:

`POST /wp-json/nero-club/v1/users/sync`

Required headers:

- `X-Nero-Timestamp` — current Unix timestamp;
- `X-Nero-Signature` — `HMAC-SHA256(timestamp + "." + raw_body, NERO_CLUB_SHARED_SECRET)`;
- `X-Nero-Idempotency-Key` — unique operation ID.

Configure the same secret in `wp-config.php` and the backend secret store:

```php
define('NERO_CLUB_SHARED_SECRET', 'replace-with-a-long-random-secret');
```

Supported actions are `create_or_activate`, `deactivate`, and `restore`. Allowed WordPress roles default to `subscriber`; the plugin never accepts an administrator role from the request. Deactivation blocks future login and destroys active sessions without deleting the user. A supplied password does not expire; a repeated `/site-access` request replaces it with a new password.

Permanent passwords are accepted only for the create/activate operation. They are not written to the plugin response beyond `password_set: true`, not logged, and must be delivered to the user by the backend over Telegram. The Google Sheet stores only login/status metadata, never the password.
