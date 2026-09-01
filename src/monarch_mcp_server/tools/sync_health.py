"""Institution connection health tools."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gql import gql

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_success, json_error

logger = logging.getLogger(__name__)

# Monarch disables GraphQL introspection for non-admin users and masks unknown
# field errors as a generic "Something went wrong", so this selection set was
# established field-by-field against the live API. Fields confirmed absent (do
# not re-add without re-testing): lastUpdateAttemptAt, dataProviderAccountId,
# dataProviderId, status, errorType.
GET_CREDENTIALS_QUERY = gql("""
query GetCredentialSyncHealth {
  credentials {
    id
    updateRequired
    disconnectedFromDataProviderAt
    syncDisabledAt
    syncDisabledReason
    dataProvider
    displayLastUpdatedAt
    institution {
      id
      name
      url
      __typename
    }
    accounts {
      id
      displayName
      deactivatedAt
      isHidden
      __typename
    }
    __typename
  }
}
""")


def _stale_days(timestamp: Optional[str]) -> Optional[int]:
    """Whole days since *timestamp*, or None if it cannot be parsed."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).days


@mcp.tool()
async def get_account_sync_health(stale_after_days: int = 3) -> str:
    """
    Report the health of each linked institution connection.

    A broken bank connection fails silently: Monarch keeps serving the last
    balances it saw and simply stops importing new transactions, so budgets and
    cashflow drift without any error surfacing. This surfaces connections that
    need re-authentication, have been disconnected by the data provider, have
    syncing disabled, or have simply gone stale.

    A connection whose accounts have all been deactivated is reported as
    dormant rather than broken. Monarch leaves the old credential behind when
    an institution is re-linked or an account is closed, and that leftover
    goes stale forever by design -- reporting it as needing attention buries
    the connections that are actually losing data.

    Args:
        stale_after_days: Flag a connection whose last successful update is at
            least this many days old. Defaults to 3.

    Returns:
        JSON with a `needs_attention` list to act on, a `dormant` list of
        leftover connections that are safe to delete, and the full connection
        list. `needs_attention` being empty means every connection still
        carrying an active account is syncing.
    """
    try:
        client = await get_monarch_client()
        result = await client.gql_call(
            operation="GetCredentialSyncHealth",
            graphql_query=GET_CREDENTIALS_QUERY,
            variables={},
        )

        connections: List[Dict[str, Any]] = []
        for cred in result.get("credentials") or []:
            institution = cred.get("institution") or {}
            stale = _stale_days(cred.get("displayLastUpdatedAt"))

            reasons = []
            if cred.get("updateRequired"):
                reasons.append("credentials need re-authentication")
            if cred.get("disconnectedFromDataProviderAt"):
                reasons.append("disconnected by the data provider")
            if cred.get("syncDisabledAt"):
                reasons.append(
                    "syncing disabled"
                    + (f": {cred['syncDisabledReason']}"
                       if cred.get("syncDisabledReason") else "")
                )
            if stale is not None and stale >= stale_after_days:
                reasons.append(f"no successful update in {stale} days")

            accounts = cred.get("accounts") or []
            active = [a for a in accounts if not a.get("deactivatedAt")]
            # No live account behind the credential means nothing is being
            # lost, however stale it looks.
            dormant = not active

            connections.append({
                "credential_id": cred.get("id"),
                "institution": institution.get("name"),
                "institution_url": institution.get("url"),
                "data_provider": cred.get("dataProvider"),
                "last_updated": cred.get("displayLastUpdatedAt"),
                "stale_days": stale,
                "update_required": bool(cred.get("updateRequired")),
                "disconnected_at": cred.get("disconnectedFromDataProviderAt"),
                "sync_disabled_at": cred.get("syncDisabledAt"),
                "sync_disabled_reason": cred.get("syncDisabledReason"),
                "dormant": dormant,
                "needs_attention": bool(reasons) and not dormant,
                "reasons": reasons,
                "active_account_count": len(active),
                "inactive_account_count": len(accounts) - len(active),
                "accounts": [a.get("displayName") for a in accounts],
                "active_accounts": [a.get("displayName") for a in active],
            })

        broken = [c for c in connections if c["needs_attention"]]
        idle = [c for c in connections if c["dormant"] and c["reasons"]]
        return json_success({
            "connection_count": len(connections),
            "needs_attention_count": len(broken),
            "dormant_count": len(idle),
            "note": (
                "A failing connection does not raise an error anywhere in "
                "Monarch -- it just stops importing transactions, so check "
                "this before trusting recent spending or cashflow numbers."
            ),
            "dormant_note": (
                "`dormant` connections look broken but have no active "
                "accounts left, so no data is being lost. They are leftovers "
                "from a re-link or a closed account and can be deleted in "
                "Monarch under Settings -> Institutions."
            ),
            "needs_attention": broken,
            "dormant": idle,
            "connections": connections,
        })
    except Exception as e:
        return json_error("get_account_sync_health", e)
