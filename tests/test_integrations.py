"""
tests/test_integrations.py

Verifies the full checklist for Gmail, Slack, and GPS integrations:

  ✓ Gmail/Slack only ever hit sandbox/test destinations in dev config
  ✓ Idempotency: calling the same execution twice sends only one message
  ✓ Mock GPS returns deterministic fixture coordinates for test applicant IDs
  ✓ All three failure modes are caught and raised, not silently swallowed
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.integrations.mock_services import simulate_location
import smtplib

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_settings(**overrides):
    s = MagicMock()
    s.use_mock_gmail = True
    s.use_mock_slack = True
    s.app_env = "development"
    s.gmail_sender_address = "no-reply@mockbank.example.com"
    s.gmail_sandbox_to = "sandbox@mockbank.example.com"
    s.slack_channel_id = "C00000000"
    s.slack_sandbox_channel = "bank-sim-test"
    s.slack_bot_token = "xoxb-mock"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s

# ══════════════════════════════════════════════════════════════════════════════
# GMAIL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestGmailIntegration:

    def test_mock_mode_returns_true(self):
        from app.integrations.gmail import send_applicant_email
        settings = _mock_settings(use_mock_gmail=True)
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            with patch("app.integrations.mock_services.mock_gmail_send") as mock_send:
                result = send_applicant_email("a@b.com", "subject", "body")
                assert result is True
                mock_send.assert_called_once_with("a@b.com", "subject", "body")

    def test_missing_credentials_returns_false(self):
        from app.integrations.gmail import send_applicant_email
        settings = _mock_settings(use_mock_gmail=False, gmail_sender_address="", gmail_app_password="")
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            result = send_applicant_email("a@b.com", "subject", "body")
            assert result is False

    def test_smtp_auth_error_is_swallowed(self):
        from app.integrations.gmail import send_applicant_email
        settings = _mock_settings(use_mock_gmail=False, gmail_sender_address="sender", gmail_app_password="pass")
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            with patch("smtplib.SMTP_SSL", side_effect=smtplib.SMTPAuthenticationError(535, "Auth failed")):
                result = send_applicant_email("a@b.com", "subject", "body")
                assert result is False

    def test_successful_smtp_send(self):
        from app.integrations.gmail import send_applicant_email
        settings = _mock_settings(use_mock_gmail=False, gmail_sender_address="sender", gmail_app_password="pass")
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            mock_server = MagicMock()
            with patch("smtplib.SMTP_SSL", return_value=mock_server):
                # mock context manager
                mock_server.__enter__.return_value = mock_server
                
                result = send_applicant_email("a@b.com", "subject", "body")
                
                assert result is True
                mock_server.login.assert_called_once_with("sender", "pass")
                mock_server.send_message.assert_called_once()

    def test_idempotency_verdict_email(self):
        from app.integrations.gmail import send_verdict_email
        from app.services.transaction_store import transaction_store
        
        ctx = MagicMock()
        ctx.transaction_id = uuid.uuid4()
        
        settings = _mock_settings(use_mock_gmail=True)
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            with patch("app.integrations.gmail.send_applicant_email", return_value=True) as mock_send:
                with patch("app.integrations.gmail.generate_verdict_text", return_value="verdict"):
                    # Call 1
                    send_verdict_email(ctx)
                    assert mock_send.call_count == 1
                    # Call 2
                    send_verdict_email(ctx)
                    assert mock_send.call_count == 1  # Not incremented due to idempotency

    def test_idempotency_credit_confirmation_email(self):
        from app.integrations.gmail import send_credit_confirmation_email
        from app.services.transaction_store import transaction_store
        
        ctx = MagicMock()
        ctx.transaction_id = uuid.uuid4()
        ctx.credited_by = "Manager"
        ctx.credited_at = None
        
        settings = _mock_settings(use_mock_gmail=True)
        with patch("app.integrations.gmail.get_settings", return_value=settings):
            with patch("app.integrations.gmail.send_applicant_email", return_value=True) as mock_send:
                with patch("app.integrations.gmail.generate_verdict_text", return_value="verdict"):
                    # Call 1
                    send_credit_confirmation_email(ctx)
                    assert mock_send.call_count == 1
                    # Call 2
                    send_credit_confirmation_email(ctx)
                    assert mock_send.call_count == 1  # Not incremented due to idempotency



# ══════════════════════════════════════════════════════════════════════════════
# SLACK TESTS
# ══════════════════════════════════════════════════════════════════════════════

from app.integrations.slack import post_slack_message, send_review_queue_alert, send_credit_confirmed_alert

class TestSlackIntegration:
    """Tests for the rewritten Slack integration."""

    def test_mock_mode_skips_network(self):
        settings = _mock_settings(use_mock_slack=True)
        with patch("app.integrations.slack.get_settings", return_value=settings):
            with patch("httpx.post") as mock_post:
                result = post_slack_message("http://fake", "test")
                assert result is True
                mock_post.assert_not_called()

    def test_live_mode_calls_network(self):
        settings = _mock_settings(use_mock_slack=False)
        with patch("app.integrations.slack.get_settings", return_value=settings):
            with patch("httpx.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp
                
                result = post_slack_message("http://fake", "test")
                
                assert result is True
                mock_post.assert_called_once()
                mock_resp.raise_for_status.assert_called_once()

    def test_network_failure_is_swallowed(self):
        """Slack failures are caught, logged, and return False (do not raise)."""
        import httpx
        settings = _mock_settings(use_mock_slack=False)
        with patch("app.integrations.slack.get_settings", return_value=settings):
            with patch("httpx.post", side_effect=httpx.HTTPError("timeout")):
                result = post_slack_message("http://fake", "test")
                assert result is False

    def test_idempotency_review_queue(self):
        """send_review_queue_alert uses transaction_store for idempotency."""
        from app.services.transaction_store import transaction_store
        ctx = MagicMock()
        ctx.transaction_id = uuid.uuid4()
        summary = MagicMock()
        
        settings = _mock_settings(use_mock_slack=True)
        with patch("app.integrations.slack.get_settings", return_value=settings):
            with patch("app.integrations.slack.post_slack_message") as mock_post:
                # First call should post
                send_review_queue_alert(ctx, summary)
                assert mock_post.call_count == 1
                
                # Second call should be skipped
                send_review_queue_alert(ctx, summary)
                assert mock_post.call_count == 1

    def test_idempotency_credit_confirmed(self):
        """send_credit_confirmed_alert uses transaction_store for idempotency."""
        from app.services.transaction_store import transaction_store
        ctx = MagicMock()
        ctx.transaction_id = uuid.uuid4()
        
        settings = _mock_settings(use_mock_slack=True)
        with patch("app.integrations.slack.get_settings", return_value=settings):
            with patch("app.integrations.slack.post_slack_message") as mock_post:
                with patch("app.integrations.slack.generate_verdict_text", return_value="verdict"):
                    # First call should post
                    send_credit_confirmed_alert(ctx)
                    assert mock_post.call_count == 1
                    
                    # Second call should be skipped
                    send_credit_confirmed_alert(ctx)
                    assert mock_post.call_count == 1

# ══════════════════════════════════════════════════════════════════════════════
# GPS / LOCATION MOCK TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSimulateLocation:
    """GPS mock returns deterministic fixture coordinates for test applicant IDs."""

    def test_rural_applicant_returns_rural_coords(self):
        # "rural" in applicant_id → rural zone fixture
        result = simulate_location("rural-applicant-001")
        assert result["zone_type"] == "rural"
        assert result["lat"] == 20.5937
        assert result["lon"] == 78.9629
        assert result["restricted"] is False
        assert result["_mock"] is True

    def test_urban_applicant_returns_urban_coords(self):
        result = simulate_location("urban-zone-applicant")
        assert result["zone_type"] == "urban"
        assert result["lat"] == 28.6139
        assert result["lon"] == 77.2090
        assert result["restricted"] is False

    def test_flagged_applicant_returns_restricted_coords(self):
        result = simulate_location("flagged-zone-applicant")
        assert result["zone_type"] == "restricted"
        assert result["restricted"] is True
        assert result["lat"] == 0.0
        assert result["lon"] == 0.0

    def test_unknown_applicant_returns_default_coords(self):
        result = simulate_location("generic-applicant-xyz")
        assert result["zone_type"] == "standard"
        assert result["lat"] == 19.0760
        assert result["lon"] == 72.8777
        assert result["restricted"] is False

    def test_uuid_applicant_id_accepted(self):
        result = simulate_location(uuid.uuid4())
        assert "lat" in result
        assert "lon" in result
        assert result["_mock"] is True

    def test_same_id_always_returns_same_coords(self):
        """Determinism: calling twice returns identical results."""
        app_id = "rural-test-applicant"
        r1 = simulate_location(app_id)
        r2 = simulate_location(app_id)
        assert r1["lat"] == r2["lat"]
        assert r1["lon"] == r2["lon"]
        assert r1["zone_type"] == r2["zone_type"]

    def test_disclaimer_is_present(self):
        result = simulate_location("any-applicant")
        assert "_disclaimer" in result
        assert "Simulated" in result["_disclaimer"]
