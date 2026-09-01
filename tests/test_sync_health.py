"""Tests for institution connection health tools."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from monarch_mcp_server.tools.sync_health import get_account_sync_health


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _cred(**overrides):
    cred = {
        "id": "cred_1",
        "updateRequired": False,
        "disconnectedFromDataProviderAt": None,
        "syncDisabledAt": None,
        "syncDisabledReason": None,
        "dataProvider": "PLAID",
        "displayLastUpdatedAt": _iso(0),
        "institution": {"id": "i1", "name": "Test Bank", "url": "https://x"},
        "accounts": [
            {"id": "a1", "displayName": "Checking",
             "deactivatedAt": None, "isHidden": False},
        ],
    }
    cred.update(overrides)
    return cred


def _client(creds):
    c = AsyncMock()
    c.gql_call.return_value = {"credentials": creds}
    return c


class TestGetAccountSyncHealth:
    """Tests for get_account_sync_health tool."""

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_healthy_connection(self, mock_get_client):
        """A freshly-synced connection needs no attention."""
        mock_get_client.return_value = _client([_cred()])

        data = json.loads(await get_account_sync_health())

        assert data["needs_attention_count"] == 0
        assert data["connections"][0]["needs_attention"] is False

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_update_required_flagged(self, mock_get_client):
        """updateRequired means the login needs re-authentication."""
        mock_get_client.return_value = _client([_cred(updateRequired=True)])

        data = json.loads(await get_account_sync_health())

        assert data["needs_attention_count"] == 1
        assert "re-authentication" in data["needs_attention"][0]["reasons"][0]

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_stale_connection_flagged(self, mock_get_client):
        """A connection that simply stopped updating is the silent failure."""
        mock_get_client.return_value = _client([_cred(displayLastUpdatedAt=_iso(9))])

        data = json.loads(await get_account_sync_health())

        conn = data["needs_attention"][0]
        assert conn["stale_days"] >= 9
        assert "no successful update" in conn["reasons"][0]

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_stale_threshold_is_configurable(self, mock_get_client):
        """A 9-day-old sync is fine if the caller allows 30 days."""
        mock_get_client.return_value = _client([_cred(displayLastUpdatedAt=_iso(9))])

        data = json.loads(await get_account_sync_health(stale_after_days=30))

        assert data["needs_attention_count"] == 0

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_multiple_reasons_accumulate(self, mock_get_client):
        """Every failure mode is reported, not just the first."""
        mock_get_client.return_value = _client([_cred(
            updateRequired=True,
            syncDisabledAt=_iso(1), syncDisabledReason="user disabled",
            displayLastUpdatedAt=_iso(40),
        )])

        reasons = json.loads(await get_account_sync_health())["needs_attention"][0]["reasons"]

        assert len(reasons) == 3
        assert any("user disabled" in r for r in reasons)

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_unparseable_timestamp_is_not_fatal(self, mock_get_client):
        """A bad timestamp yields stale_days=None rather than raising."""
        mock_get_client.return_value = _client([_cred(displayLastUpdatedAt="nonsense")])

        data = json.loads(await get_account_sync_health())

        assert data["connections"][0]["stale_days"] is None

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_dormant_connection_not_flagged(self, mock_get_client):
        """A stale credential whose accounts are all deactivated is leftover.

        Monarch keeps the old credential when an institution is re-linked, and
        it then goes stale forever. Reporting it buries the connections that
        are really losing data.
        """
        mock_get_client.return_value = _client([_cred(
            updateRequired=True,
            displayLastUpdatedAt=_iso(500),
            accounts=[{"id": "a1", "displayName": "Old Checking",
                       "deactivatedAt": "2025-05-01", "isHidden": True}],
        )])

        data = json.loads(await get_account_sync_health())

        assert data["needs_attention_count"] == 0
        assert data["dormant_count"] == 1
        conn = data["dormant"][0]
        assert conn["dormant"] is True
        assert conn["active_account_count"] == 0
        assert conn["inactive_account_count"] == 1
        # the reasons are still reported, just not as an action item
        assert len(conn["reasons"]) == 2

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_credential_with_no_accounts_is_dormant(self, mock_get_client):
        """A credential Monarch left behind with zero accounts is not broken."""
        mock_get_client.return_value = _client([_cred(
            updateRequired=True, displayLastUpdatedAt=_iso(100), accounts=[],
        )])

        data = json.loads(await get_account_sync_health())

        assert data["needs_attention_count"] == 0
        assert data["dormant_count"] == 1

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_one_live_account_still_flags_the_connection(self, mock_get_client):
        """A single surviving account means the connection still matters."""
        mock_get_client.return_value = _client([_cred(
            updateRequired=True,
            displayLastUpdatedAt=_iso(500),
            accounts=[
                {"id": "a1", "displayName": "Closed",
                 "deactivatedAt": "2025-05-01", "isHidden": True},
                {"id": "a2", "displayName": "Live IRA",
                 "deactivatedAt": None, "isHidden": False},
            ],
        )])

        data = json.loads(await get_account_sync_health())

        assert data["needs_attention_count"] == 1
        conn = data["needs_attention"][0]
        assert conn["dormant"] is False
        assert conn["active_accounts"] == ["Live IRA"]
        assert conn["inactive_account_count"] == 1

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_healthy_dormant_connection_is_not_listed(self, mock_get_client):
        """Dormant only lists credentials that would otherwise be flagged."""
        mock_get_client.return_value = _client([_cred(
            accounts=[{"id": "a1", "displayName": "Closed",
                       "deactivatedAt": "2025-05-01", "isHidden": True}],
        )])

        data = json.loads(await get_account_sync_health())

        assert data["dormant_count"] == 0
        assert data["needs_attention_count"] == 0

    @patch('monarch_mcp_server.tools.sync_health.get_monarch_client')
    async def test_error_handling(self, mock_get_client):
        """Transport failures are reported as tool errors."""
        c = AsyncMock()
        c.gql_call.side_effect = Exception("boom")
        mock_get_client.return_value = c

        data = json.loads(await get_account_sync_health())

        assert data["error"] is True
        assert data["tool"] == "get_account_sync_health"
