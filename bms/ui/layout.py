"""Shared shell for the operator interface.

Server-rendered HTML with small islands of vanilla JavaScript, rather than a
single-page app. For a handful of screens over 25 devices, a build step, a
node_modules tree and a bundle to deploy would be more machinery than the problem
needs -- and this has to run unattended on a Mac mini where every extra moving
part is another thing that can fail at 3am.

That choice is cheap to revisit: the JSON API is complete and role-enforced on its
own, so a React front end could be added later against the same endpoints without
touching the backend.

Pages call the JSON API through `postJSON` below rather than posting forms to
bespoke UI routes. That means authorisation lives in exactly one place -- the API
dependencies -- and the UI cannot accidentally grant something the API refuses.
"""

from __future__ import annotations

from html import escape
from typing import Any

# Kept in one place so the operator UI, the login form and the tenant page stay
# visually consistent without a CSS build.
CSS = """
*{box-sizing:border-box}
:root{
  --bg:#fff; --fg:#111; --muted:#666; --line:#e3e3e8; --card:#f6f6f8;
  --accent:#1a73e8; --ok:#137333; --warn:#b06000; --bad:#c5221f; --chip:#eceff4;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0d0d0f; --fg:#f2f2f4; --muted:#9a9aa2; --line:#2c2c33; --card:#17171b;
  --accent:#8ab4f8; --ok:#81c995; --warn:#fdd663; --bad:#f28b82; --chip:#22222a;
}}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     margin:0;background:var(--bg);color:var(--fg)}
a{color:var(--accent)}
header{border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
.bar{max-width:64rem;margin:0 auto;padding:.75rem 1.25rem;display:flex;
     align-items:center;gap:1.25rem;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.01em;margin-right:auto}
nav{display:flex;gap:1rem;flex-wrap:wrap}
nav a{text-decoration:none;color:var(--muted);padding:.2rem 0;border-bottom:2px solid transparent}
nav a:hover{color:var(--fg)}
nav a.on{color:var(--fg);border-bottom-color:var(--accent)}
.who{color:var(--muted);font-size:.85rem}
main{max-width:64rem;margin:0 auto;padding:1.5rem 1.25rem 4rem}
h1{font-size:1.5rem;margin:.5rem 0 .25rem}
h2{font-size:1.05rem;margin:2rem 0 .75rem}
.lede{color:var(--muted);margin:0 0 1.5rem}
.card{background:var(--card);border-radius:14px;padding:1.1rem 1.25rem;margin-bottom:1rem}
.grid{display:grid;gap:1rem}
@media(min-width:40rem){.grid.two{grid-template-columns:1fr 1fr}
                        .grid.three{grid-template-columns:repeat(3,1fr)}}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.55rem .6rem;border-bottom:1px solid var(--line);
      vertical-align:middle}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}
.chip{display:inline-block;padding:.12em .55em;border-radius:999px;font-size:.78rem;
      background:var(--chip);color:var(--muted);white-space:nowrap}
.chip.ok{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.chip.warn{background:color-mix(in srgb,var(--warn) 20%,transparent);color:var(--warn)}
.chip.bad{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
button,.btn{font:inherit;padding:.5rem .9rem;border-radius:10px;border:1px solid var(--line);
     background:var(--bg);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}
button.danger{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 35%,var(--line))}
button[disabled]{opacity:.5;cursor:default}
input,select{font:inherit;padding:.45rem .6rem;border-radius:9px;border:1px solid var(--line);
             background:var(--bg);color:var(--fg)}
input[type=number]{width:6rem}
label{font-size:.85rem;color:var(--muted);display:block;margin-bottom:.25rem}
.row{display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap}
.big{font-size:2rem;font-weight:650;line-height:1.1;font-variant-numeric:tabular-nums}
.sub{color:var(--muted);font-size:.85rem}
.empty{color:var(--muted);padding:1.5rem 0;text-align:center}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:1.5rem;z-index:50;
       padding:.7rem 1.1rem;border-radius:12px;display:none;font-size:.92rem;
       box-shadow:0 4px 18px #0003;max-width:90vw}
#toast.ok{display:block;background:var(--ok);color:#fff}
#toast.err{display:block;background:var(--bad);color:#fff}
"""

# One helper used by every page. Deliberately never claims a write failed: the
# thermostats sometimes apply a value without acknowledging it, so an error here
# means "unknown", not "rejected".
JS = """
function toast(msg, ok){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = ok ? 'ok' : 'err';
  clearTimeout(window.__tt);
  window.__tt = setTimeout(() => { t.className = ''; }, ok ? 2600 : 7000);
}
async function api(method, path, body){
  const res = await fetch(path, {
    method,
    headers: body ? {'content-type':'application/json'} : {},
    body: body ? JSON.stringify(body) : null
  });
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try { const j = await res.json();
          detail = (j.detail && j.detail.detail) || j.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json().catch(() => null);
}
async function act(method, path, body, okMsg, reload){
  try {
    await api(method, path, body);
    toast(okMsg || 'Done', true);
    if (reload !== false) setTimeout(() => location.reload(), 700);
  } catch (err) {
    toast(err.message, false);
  }
}
"""

NAV = (
    ("/", "Dashboard", "tenant"),
    ("/ui/zones", "Zones", "manager"),
    ("/ui/schedules", "Schedules", "manager"),
    ("/ui/holidays", "Holidays", "manager"),
    ("/ui/system", "System", "manager"),
    ("/ui/users", "Users", "admin"),
)


def e(value: Any) -> str:
    """Escape for HTML. Device and holiday names are user-supplied."""
    return escape("" if value is None else str(value), quote=True)


def num(value: Any, suffix: str = "", places: int = 1, dash: str = "—") -> str:
    if value is None:
        return dash
    if isinstance(value, (int, float)):
        return f"{value:.{places}f}{suffix}"
    return e(value)


def chip(text: str, kind: str = "") -> str:
    return f'<span class="chip {kind}">{e(text)}</span>'


# Inline SVG rather than an icon font or emoji: the page has to stay
# self-contained, and emoji render inconsistently across phones and desktops --
# a flame that shows as a coloured square on one device is worse than no icon.
_ICONS = {
    "cool": (
        '<path d="M12 2v20M4.2 7l15.6 9M19.8 7L4.2 16M12 6.5 9.5 4M12 6.5 14.5 4'
        'M12 17.5 9.5 20M12 17.5 14.5 20"/>'
    ),
    "heat": (
        '<path d="M12 22c3.3 0 6-2.6 6-5.9 0-4-3-5.900-3-9.6 0-1.2-1-2.5-3-2.5'
        ' 1 3.4-1.2 4.9-2.6 6.6C8 12.3 6 13.8 6 16.1 6 19.4 8.7 22 12 22Z"/>'
    ),
    # A four-blade fan, drawn so the shape still reads at 14px.
    "fan": (
        '<circle cx="12" cy="12" r="2"/>'
        '<path d="M12 10c0-3 .8-6 3.2-6 1.8 0 2.6 2.4.6 4.2C14.4 9.3 13 9.8 12 10Z"/>'
        '<path d="M14 12c3 0 6 .8 6 3.2 0 1.8-2.4 2.6-4.2.6C14.7 14.4 14.2 13 14 12Z"/>'
        '<path d="M12 14c0 3-.8 6-3.2 6-1.8 0-2.6-2.4-.6-4.2C9.6 14.7 11 14.2 12 14Z"/>'
        '<path d="M10 12c-3 0-6-.8-6-3.2 0-1.8 2.4-2.6 4.2-.6C9.3 9.6 9.8 11 10 12Z"/>'
    ),
    "econ": '<path d="M4 20c0-8 6-14 16-15 0 10-6 15-13 15H4Zm3 0c2-5 5-8 9-10"/>',
}

ICON_CSS = """
.act{display:inline-flex;align-items:center;gap:.22rem;margin-right:.5rem;
     font-size:.8rem;font-variant-numeric:tabular-nums;white-space:nowrap}
.act svg{width:15px;height:15px;flex:none}
.act.cool{color:#1565d8} .act.heat{color:#d1611a}
.act.fan{color:var(--muted)} .act.econ{color:#137333}
@media(prefers-color-scheme:dark){.act.cool{color:#7fb0ff} .act.heat{color:#ffab6b}
  .act.econ{color:#81c995}}
.act.spin svg{animation:spin 2.6s linear infinite;transform-origin:50% 50%}
@keyframes spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.act.spin svg{animation:none}}
.idle{color:var(--muted);font-size:.8rem}
"""


def icon(kind: str, label: str = "", spin: bool = False) -> str:
    """One activity indicator. `label` is drawn beside it, e.g. a stage count."""
    path = _ICONS.get(kind, "")
    classes = f"act {kind}" + (" spin" if spin else "")
    text = f"<span>{e(label)}</span>" if label else ""
    return (
        f'<span class="{classes}" title="{e(kind)}">'
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
        f' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>'
        f"{text}</span>"
    )


def activity(values: dict) -> str:
    """Render what the equipment is doing, from the polled stage counts.

    Deliberately driven by active stages rather than by the selected mode: a unit
    sitting in HEAT with nothing energised is idle, and showing a flame for it
    would make the whole dashboard look busy at all times.
    """

    def count(key: str) -> int:
        v = values.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    out = []
    cool, heat, aux = count("active_cool_stages"), count("active_heat_stages"), count(
        "active_aux_heat_stages"
    )
    if cool:
        out.append(icon("cool", str(cool) if cool > 1 else ""))
    if heat:
        out.append(icon("heat", str(heat) if heat > 1 else ""))
    if aux:
        out.append(icon("heat", f"aux {aux}" if aux > 1 else "aux"))
    if values.get("economizer_enabled"):
        out.append(icon("econ"))
    if values.get("fan_running"):
        out.append(icon("fan", spin=True))

    if not out:
        return '<span class="idle">idle</span>' if values else "—"
    return "".join(out)


# A data URI rather than a served file, so the pages stay self-contained and a
# favicon request can never 404 into the logs or block on the network. Drawn as a
# thermostat dial: recognisable at 16px, and distinct enough that an operator with
# several tabs open can find this one.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%231a73e8'/%3E"
    "%3Ccircle cx='16' cy='16' r='9' fill='none' stroke='white' stroke-width='2.5'"
    " stroke-dasharray='42 100' transform='rotate(140 16 16)'/%3E"
    "%3Ccircle cx='16' cy='16' r='3.2' fill='white'/%3E"
    "%3C/svg%3E"
)


def page(title: str, user: Any, body: str, active: str = "") -> str:
    """Wrap page content in the shared shell.

    Nav entries are filtered by role so a tenant is not shown links that would
    only 403 -- the API still enforces it, this just avoids dead ends.
    """
    links = "".join(
        f'<a href="{e(path)}" class="{"on" if path == active else ""}">{e(label)}</a>'
        for path, label, role in NAV
        if user is not None and user.at_least(role)
    )
    who = (
        f'<span class="who">{e(user.display_name or user.username)} · {e(user.role)}</span>'
        if user
        else ""
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · building-controls</title>
<link rel="icon" href="{FAVICON}">
<style>{CSS}{ICON_CSS}</style>
<header><div class="bar">
  <span class="brand">building-controls</span>
  <nav>{links}</nav>
  {who}
  <form method="post" action="/logout" style="margin:0">
    <button type="submit" style="padding:.3rem .7rem;font-size:.85rem">Sign out</button>
  </form>
</div></header>
<main>{body}</main>
<div id="toast"></div>
<script>{JS}</script>
"""
