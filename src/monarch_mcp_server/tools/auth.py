"""Authentication tools."""

import logging
import os

from mcp.server.fastmcp import Context

from monarch_mcp_server import auth
from monarch_mcp_server.app import mcp
from monarch_mcp_server.secure_session import secure_session

logger = logging.getLogger(__name__)


@mcp.tool()
async def setup_authentication() -> str:
    """Get instructions for setting up secure authentication with Monarch Money."""
    return """🔐 Monarch Money - Authentication Options

Option 1: Elicitation login (Recommended for interactive clients)
   Call 'monarch_login' to enter email/password (and MFA if needed)
   via a secure form in your client UI. Credentials never pass
   through the model. Or 'monarch_login_with_token' to paste a
   browser-copied session token.

Option 2: Email/Password (Terminal)
   Run in terminal: python login_setup.py
   Enter the email verification code if Monarch sends one (this can
   happen for a new device/session even when MFA is off), and your
   2FA code if MFA is enabled.

Call 'monarch_logout' to clear the stored session.

✅ Session persists across restarts
✅ Token stored securely in system keyring"""


@mcp.tool()
async def monarch_login(ctx: Context) -> str:
    """Sign in to Monarch Money.

    Opens a secure form in the client UI to collect email, password, and
    (if required) an MFA code. Credentials never pass through the model —
    they flow client-UI → server directly via the MCP protocol.
    """
    return await auth.login_interactive(ctx)


@mcp.tool()
async def monarch_login_with_token(ctx: Context) -> str:
    """Sign in to Monarch Money using a browser-copied session token.

    Useful for SSO users who can't use password login. Grab the token from
    browser DevTools → Application → Local Storage → app.monarchmoney.com.
    """
    return await auth.login_with_token_interactive(ctx)


@mcp.tool()
async def monarch_logout() -> str:
    """Clear the stored Monarch Money session from the system keyring."""
    return await auth.logout()


@mcp.tool()
async def check_auth_status() -> str:
    """Check if already authenticated with Monarch Money."""
    try:
        session = secure_session.load_session()
        if not session:
            status = "❌ No stored session found in keyring\n"
        else:
            if session.get("auth_mode") == "cookie":
                count = len(session.get("cookies") or {})
                status = (
                    "✅ Cookie session found in secure keyring storage "
                    f"({count} cookies)\n"
                )
            else:
                status = "✅ Token session found in secure keyring storage\n"
            if session.get("device_uuid"):
                status += "🔑 Device UUID recorded\n"

        email = os.getenv("MONARCH_EMAIL")
        if email:
            status += f"📧 Environment email: {email}\n"

        status += (
            "\n💡 A stored session can still be expired — Monarch then answers "
            "401 Unauthorized. Call get_accounts to confirm it still works, or "
            "run login_setup.py to re-authenticate."
        )

        return status
    except Exception as e:
        return f"Error checking auth status: {str(e)}"


@mcp.tool()
async def debug_session_loading() -> str:
    """Debug keyring session loading issues."""
    try:
        session = secure_session.load_session()
        if not session:
            return "❌ No session found in keyring. Run login_setup.py to authenticate."

        auth_mode = session.get("auth_mode", "token")
        parts = [f"✅ Session found in keyring (auth_mode: {auth_mode})."]
        if session.get("token"):
            parts.append("Carries a session token.")
        cookies = session.get("cookies") or {}
        if cookies:
            parts.append(f"Carries {len(cookies)} session cookies.")
        if session.get("device_uuid"):
            parts.append("Device UUID recorded.")
        parts.append(
            "Loading a session does not prove it is still valid — call "
            "get_accounts to check for a 401."
        )
        return " ".join(parts)
    except Exception as e:
        logger.exception("Keyring access failed")
        return f"❌ Keyring access failed: {type(e).__name__}: {e}"
