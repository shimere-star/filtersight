# Filtersight A2P campaign submission draft

Use this as the campaign's “How do end-users consent to receive messages?” description. It is written to match the deployed signup flow and the public consent details page. Check the URLs and current campaign fields in Twilio before submitting.

## Message flow / opt-in description

> Customers buy the Filter + Companion ($10/month) or Complete ($13/month) subscription at https://filtersight.com/. After checkout, the setup page asks the customer to enter their own mobile number and separately select an unchecked, optional SMS consent checkbox. The checkbox says: “I agree to receive recurring SMS messages from Filtersight for encouragement and account support. Message frequency varies. Msg & data rates may apply. Reply STOP to opt out, HELP for help. Consent isn't required to buy the plan.” Customers may complete setup and use the service without opting into SMS. Tier 1 does not collect a phone number or send SMS. The public consent-flow details are at https://filtersight.com/sms-consent.html. Privacy Policy: https://filtersight.com/privacy.html. Terms: https://filtersight.com/terms.html. For the Complete plan, a customer may separately provide an accountability partner's number. Filtersight sends one invitation asking the partner to reply YES to opt in or STOP to decline. No partner alerts are sent unless the partner replies YES. The partner may reply STOP to opt out or HELP for help.

## Sample messages

Provide examples that match the currently enabled message categories. If the submitted campaign doesn't include partner invitations and alerts, remove samples 2 and 3 from the submission.

1. `Filtersight: [short encouragement message]. Reply STOP to opt out or HELP for help.`
2. `Filtersight: Someone invited you to receive accountability alerts for their Filtersight plan. Reply YES to opt in or STOP to decline. Msg & data rates may apply. Reply HELP for help.`
3. `Filtersight: Your accountability partner had a filter bypass attempt. Reply STOP to opt out or HELP for help.`

## Before submitting

- Ensure the public URLs load without authentication and show the exact disclosure and policies.
- If Twilio's reviewer cannot reach the post-checkout setup form, provide a publicly accessible screenshot of the actual form with its unchecked checkbox and phone fields. The public page above documents the same flow but is not itself an opt-in form.
- Confirm the campaign description, use case, sample messages, and any “embedded links / phone numbers / age-gated content” checkboxes truthfully match the texts being sent.
- Keep `ENABLE_SMS=false` until Twilio approves the campaign and the correct sender is associated with it.
- Before reopening paid Tier 2/3 sales, configure one scheduled caller for `POST /poll-nextdns-and-notify` every five minutes with `X-Admin-Secret` set to `BACKFILL_ADMIN_SECRET`. Keep the feature flag off until the scheduler is ready.
- Verify NextDNS profile creation, category settings, logging, and retention in the production account before reopening paid Tier 2/3 sales.

Tier 2/3 remain hidden unless `ENABLE_TIER2_TIER3=true` on both Railway services. SMS sending has a separate opt-in configuration gate and defaults to disabled.
