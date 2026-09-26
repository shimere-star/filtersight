# Filtersight A2P campaign consent description

Use this as the campaign's “How do end-users consent to receive messages?” description. Update the public URLs if the deployed policy pages differ.

> Customers first select and pay for a Filtersight subscription through Stripe checkout. After successful checkout, customers on the $10 Companion or $13 Complete plan may enter their own mobile number on the Filtersight setup page and separately check an unchecked, optional SMS consent box. The page states that recurring encouragement and account-support messages are sent by Filtersight, message frequency varies, message and data rates may apply, and the customer can reply STOP to opt out or HELP for help. SMS consent is not required to purchase. Our Privacy Policy and Terms are linked next to the consent. Tier 1 does not request a phone number or send SMS. For Complete plan partner alerts, the customer separately enters the partner's number; Filtersight sends only an invitation asking that person to reply YES to opt in or STOP to decline. No partner alerts are sent until YES is received. The partner can reply STOP at any time.

## Example consent disclosure on the signup page

> I agree to receive recurring SMS messages from Filtersight for encouragement and account support. Message frequency varies. Msg & data rates may apply. Reply STOP to opt out, HELP for help. Consent isn't required to buy the plan.

Keep the form behavior and published policy pages consistent with the description. Do not submit this campaign until the consent collection flow is deployed and publicly inspectable for Twilio's review. Tier 2/3 sales are gated off by default; arrange reviewer access to the actual unchecked checkbox and phone collection flow without enabling customer checkout. Twilio campaign review and approval remain a manual step; code changes do not grant A2P approval.

## Operational items before enabling text delivery

- Submit/resubmit the A2P campaign in Twilio with the live signup and policy URLs.
- Confirm Twilio has approved the campaign and the sender is associated with it.
- Configure a single scheduled caller for `POST /poll-nextdns-and-notify` every five minutes, with the `X-Admin-Secret` header set to the backend's `BACKFILL_ADMIN_SECRET`.
- Keep that schedule disabled until Twilio has approved messaging. The endpoint is protected and the profile polling is per paid Tier 2/3 subscription.
- The signup and backend enforce `ENABLE_TIER2_TIER3`; it defaults to false. With the flag off, Tier 2/3 are hidden and their API/SMS features are unavailable. Turn it on only after approval and scheduler setup.
- NextDNS documents its API as beta; verify profile creation, parental-control category settings, log retention, and log reasons against the production account before reopening paid Tier 2/3 sales.

The poll endpoint detects blocked attempts from NextDNS log entries with status `blocked` and a reason containing `porn`. As with any DNS filtering, this is domain-level detection and can miss content hosted on general-purpose domains.
