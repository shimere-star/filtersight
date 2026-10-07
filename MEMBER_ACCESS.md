# Filtersight member access

The signup app includes a simple member dashboard at:

`https://signup-app-v3-production.up.railway.app/?view=member`

Members enter the email used at checkout and receive a one-time sign-in link. The backend only sends a link for a customer with an active Stripe subscription, consumes the link once, then creates a hashed 30-day member session. Each member API request rechecks the subscription with Stripe. The dashboard shows cancellation for both paid tiers and the AI companion for Tier 2 customers.

## Railway email settings

Member sign-in requires an outbound SMTP provider. Configure these on `webhook-server-v11` in Railway:

- `SMTP_HOST`
- `SMTP_PORT` (587 for STARTTLS or 465 for implicit SSL)
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL` (a sender address authorized by the provider)
- `SMTP_USE_STARTTLS` (`true` for port 587; defaults to true)
- `SMTP_USE_SSL` (`true` for port 465; defaults to false)

No SMTP provider is currently configured. Until these variables are set, sign-in-link requests return a configuration error and members cannot use this dashboard. Keep the provider credential secret in Railway; do not commit it to GitHub.

Tier 2 requires a configured Anthropic API key for the companion. No messaging provider or DNS-log polling schedule is used.
