import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import requests

from streamlit.testing.v1 import AppTest
import marketing_analytics as analytics

ROOT = Path(__file__).resolve().parents[1]


class MarketingAnalyticsTests(unittest.TestCase):
    def test_only_reviewed_campaign_values_survive(self):
        self.assertEqual(analytics.campaign_tags({
            "utm_source": "x", "utm_medium": "paid_social", "utm_campaign": "launch",
            "utm_content": "alice@example.com", "email": "private@example.com",
            "session_id": "cs_secret", "token": "magic-secret", "utm_term": "private search"
        }), {"utm_source": "x", "utm_medium": "paid_social", "utm_campaign": "launch"})

    def test_html_has_no_raw_checkout_identifiers_or_unreviewed_data(self):
        config = {**analytics.CONFIG, "website_id": "11111111-1111-4111-8111-111111111111"}
        with patch.object(analytics, "CONFIG", config):
            markup = analytics.analytics_html("Paid signup", {
                "utm_source": "x", "utm_campaign": "</script><script>alert(1)</script>",
                "email": "private@example.com"
            }, "paid:cs_sensitive")
        self.assertNotIn("cs_sensitive", markup)
        self.assertNotIn("private@example.com", markup)
        self.assertNotIn("alert(1)", markup)
        self.assertIn('"utm_source": "x"', markup)

    def test_missing_account_disables_emission(self):
        with patch.dict(analytics.CONFIG, {"website_id": ""}):
            self.assertEqual(analytics.analytics_html("Signup viewed", {}, "signup"), "")

    def checkout(self, params=None, fail_analytics=False):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        for key, value in (params or {}).items():
            app.query_params[key] = value
        with (
            patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_PRICE_ID": "price_10"}),
            patch("stripe.checkout.Session.create", return_value=types.SimpleNamespace(url="https://checkout.example/test")) as create,
            patch("marketing_analytics.analytics_html", side_effect=RuntimeError("unavailable") if fail_analytics else None, return_value=""),
        ):
            app.run()
            app.text_input[0].set_value("member@example.com")
            app.button[0].click().run()
        self.assertFalse(list(app.exception))
        return create.call_args.kwargs

    def test_campaign_reaches_checkout_without_changing_billing(self):
        result = self.checkout({"utm_source": "x", "utm_medium": "paid_social", "utm_campaign": "launch", "utm_content": "bad@example.com"})
        self.assertEqual(result["metadata"], {"plan": "filtersight", "utm_source": "x", "utm_medium": "paid_social", "utm_campaign": "launch"})
        self.assertEqual(result["subscription_data"], {"metadata": {"plan": "filtersight"}})
        self.assertEqual(result["line_items"], [{"price": "price_10", "quantity": 1}])
        self.assertEqual(result["mode"], "subscription")

    def test_opt_out_drops_stripe_campaigns(self):
        self.assertEqual(self.checkout({"utm_source": "x", "fs_analytics": "off"})["metadata"], {"plan": "filtersight"})

    def test_analytics_failure_does_not_prevent_checkout(self):
        self.assertEqual(self.checkout(fail_analytics=True)["mode"], "subscription")

    def test_unpaid_session_never_emits_paid_signup(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        app.query_params["session_id"] = "cs_unpaid"
        session = types.SimpleNamespace(payment_status="unpaid", status="complete", mode="subscription",
            customer_details=types.SimpleNamespace(email="member@example.com"),
            metadata=types.SimpleNamespace(to_dict=lambda: {"plan": "filtersight"}))
        with patch("stripe.checkout.Session.retrieve", return_value=session), patch("marketing_analytics.analytics_html", return_value="") as render:
            app.run()
        self.assertFalse(list(app.exception))
        self.assertFalse(any(call.args[0] == "Paid signup" for call in render.call_args_list))

    def test_verified_paid_session_emits_only_reviewed_metadata(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        app.query_params["session_id"] = "cs_paid"
        session = types.SimpleNamespace(payment_status="paid", status="complete", mode="subscription",
            customer_details=types.SimpleNamespace(email="member@example.com"),
            metadata=types.SimpleNamespace(to_dict=lambda: {"plan": "filtersight", "utm_source": "x"}))
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("Unavailable")
        with patch("stripe.checkout.Session.retrieve", return_value=session), patch("requests.post", return_value=response), patch("marketing_analytics.analytics_html", return_value="") as render:
            app.run()
        self.assertFalse(list(app.exception))
        paid = [call for call in render.call_args_list if call.args[0] == "Paid signup"]
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid[0].args[1]["utm_source"], "x")


if __name__ == "__main__":
    unittest.main()
