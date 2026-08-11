"""HTML for the onboarding flow. Norwegian, plain, mobile-first.

Written as small functions returning complete documents rather than as
templates: there are four pages, and a template engine would be one more
dependency and one more thing to deploy.
"""
import html

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 2rem 1.25rem; display: flex; justify-content: center;
}
main { width: 100%; max-width: 34rem; }
h1 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; }
label { display: block; font-weight: 600; margin: 1rem 0 .35rem; }
input[type=email], input[type=password], input[type=text] {
  width: 100%; padding: .7rem .8rem; font-size: 1rem; border-radius: .5rem;
  border: 1px solid rgba(128,128,128,.5); background: transparent; color: inherit;
}
button {
  margin-top: 1.5rem; width: 100%; padding: .8rem 1rem; font-size: 1rem;
  font-weight: 600; border: 0; border-radius: .5rem; cursor: pointer;
  background: #0b6bcb; color: #fff;
}
.consent, .note {
  border: 1px solid rgba(128,128,128,.35); border-radius: .6rem;
  padding: .9rem 1rem; margin: 1.5rem 0; font-size: .92rem;
}
.consent ul { margin: .5rem 0 0; padding-left: 1.2rem; }
.error {
  border-left: 4px solid #c0392b; background: rgba(192,57,43,.09);
  padding: .8rem 1rem; border-radius: .3rem; margin: 1rem 0;
}
code, .url {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88rem;
  word-break: break-all;
}
.url {
  display: block; padding: .8rem; margin: .6rem 0 0;
  background: rgba(128,128,128,.13); border-radius: .5rem;
}
ol { padding-left: 1.2rem; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"no\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _error_block(error: str | None) -> str:
    return f'<p class="error">{html.escape(error)}</p>' if error else ""


CONSENT = """
<div class="consent">
  <strong>Dette lagres om deg</strong>
  <ul>
    <li>En <em>innloggingsnøkkel</em> fra Garmin (ikke passordet ditt), på serveren.</li>
    <li>Passordet ditt sendes videre til Garmin for innlogging, og forsvinner
        fra serveren i samme øyeblikk. Det lagres aldri, og skrives aldri i noen logg.</li>
    <li>Ingen treningsdata lagres her — de hentes direkte fra Garmin når du spør.</li>
  </ul>
  <p style="margin:.7rem 0 0">
    Du får en personlig adresse til slutt. <strong>Den som har adressen, ser
    treningsdataene dine</strong> — så del den ikke.
    Vil du slette alt? Si fra til Sindre, så fjernes nøkkelen din, og adressen
    slutter å virke umiddelbart.
  </p>
</div>
"""


def login_page(error: str | None = None) -> str:
    return _page(
        "Koble Garmin til AI-assistenten din",
        f"""
        <h1>Koble Garmin til AI-assistenten din</h1>
        <p>Logg inn med din egen Garmin Connect-konto. Du får en personlig
        adresse du limer inn i ChatGPT eller Claude, og kan spørre om dine egne
        treningsdata.</p>
        {CONSENT}
        {_error_block(error)}
        <form method="post" action="start">
          <label for="email">Garmin Connect e-post</label>
          <input id="email" name="email" type="email" required autocomplete="username"
                 autocapitalize="none" spellcheck="false">
          <label for="password">Passord</label>
          <input id="password" name="password" type="password" required
                 autocomplete="current-password">
          <button type="submit">Logg inn</button>
        </form>
        """,
    )


def mfa_page(session_id: str, error: str | None = None) -> str:
    return _page(
        "Engangskode fra Garmin",
        f"""
        <h1>Engangskode</h1>
        <p>Garmin har sendt deg en kode på e-post eller SMS. Skriv den inn her.
        Du har fem minutter.</p>
        {_error_block(error)}
        <form method="post" action="mfa">
          <input type="hidden" name="session_id" value="{html.escape(session_id)}">
          <label for="code">Kode</label>
          <input id="code" name="code" type="text" required inputmode="numeric"
                 autocomplete="one-time-code" pattern="[0-9]*">
          <button type="submit">Fullfør</button>
        </form>
        """,
    )


def success_page(connector_url: str) -> str:
    safe_url = html.escape(connector_url)
    return _page(
        "Ferdig — her er adressen din",
        f"""
        <h1>Ferdig 🎉</h1>
        <p>Dette er din personlige adresse:</p>
        <code class="url">{safe_url}</code>
        <div class="note">
          <strong>Ta vare på den.</strong> Den vises bare nå, og den som har den,
          ser treningsdataene dine. Lagre den et trygt sted — ikke i en åpen chat.
        </div>
        <h2>Slik legger du den inn i Claude</h2>
        <ol>
          <li>Innstillinger → Connectors → «Add custom connector»</li>
          <li>Lim inn adressen over, og gi den et navn (f.eks. «Garmin»)</li>
          <li>Spør: «Hvordan var treningsuka mi?»</li>
        </ol>
        <h2>Slik legger du den inn i ChatGPT</h2>
        <ol>
          <li>Innstillinger → Connectors → legg til egendefinert connector (MCP)</li>
          <li>Lim inn den samme adressen</li>
        </ol>
        <p class="note">Angrer du? Si fra til Sindre, så slettes nøkkelen din og
        adressen slutter å virke.</p>
        """,
    )


def expired_page() -> str:
    return _page(
        "Innlogging utløpt",
        """
        <h1>Innloggingen utløp</h1>
        <p>Det tok for lang tid, eller koden var feil. Begge deler betyr at du må
        begynne på nytt — Garmin krever en ny kode.</p>
        <p><a href=".">Prøv igjen</a></p>
        """,
    )
