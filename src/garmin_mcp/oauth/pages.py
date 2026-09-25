"""HTML for the consent step and for errors a person sees in the browser.

The consent page is where the AI-transparency statement and explicit consent
live (Garmin Connect Developer Program agreement §15.10). Every value that
comes from outside — the client's self-registered name, its redirect host —
is escaped: dynamic client registration lets anyone choose those strings.
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlparse

_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}"
    "h1{font-size:1.5rem}ul{padding-left:1.2rem}small{color:#555}"
    "button{font:inherit;padding:.6rem 1.2rem;margin-right:.5rem;cursor:pointer}"
    ".error{color:#a00}"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>"
    )


def consent_page(
    *,
    client_name: str,
    redirect_uri: str,
    request_id: str,
    action: str,
    privacy_url: str,
    operator: str,
    error: str | None = None,
) -> str:
    name = escape(client_name)
    host = escape(urlparse(redirect_uri).hostname or redirect_uri)
    error_html = f"<p class='error'>{escape(error)}</p>" if error else ""
    body = f"""
<h1>Connect your Garmin data to {name}</h1>
<p>Garmin MCP lets <strong>{name}</strong> read your own Garmin Connect data, so it can answer
questions about your training, sleep and recovery. After you continue, Garmin asks you which
data to share.</p>
<ul>
<li><strong>What we store:</strong> the activity and health summaries you allow Garmin to share,
such as activities, daily steps and heart rate, sleep, stress and HRV. We store them on servers
in the EU.</li>
<li><strong>AI:</strong> when you ask {name} a question, the data needed to answer it is sent to
that AI service and handled under its terms. We never send your Garmin data to an AI service on
our own, never use it to train AI models, and never sell it.</li>
<li><strong>Stop at any time:</strong> remove Garmin MCP under Connected Apps in Garmin Connect.
We then delete your stored data.</li>
</ul>
<p><small>You will be sent back to <code>{host}</code>.</small></p>
{error_html}
<form method="post" action="{escape(action)}">
<input type="hidden" name="request" value="{escape(request_id)}">
<p><label><input type="checkbox" name="consent" value="yes" required>
I agree that my Garmin data is processed as described here and in the
<a href="{escape(privacy_url)}" target="_blank" rel="noopener">privacy policy</a>.</label></p>
<button type="submit" name="decision" value="approve">Continue to Garmin</button>
<button type="submit" name="decision" value="deny" formnovalidate>Cancel</button>
</form>
<p><small>Garmin MCP is operated by {escape(operator)}. Garmin and Garmin Connect are trademarks
of Garmin Ltd. or its subsidiaries.</small></p>
"""
    return _page(f"Connect Garmin to {client_name}", body)


def message_page(title: str, message: str) -> str:
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p>")


EXPIRED = (
    "Sign-in link expired",
    "This sign-in link has expired or was already used. Start again from your AI assistant.",
)
