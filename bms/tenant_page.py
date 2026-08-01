"""The tenant-facing bypass page.

Designed for a phone held one-handed, not a desktop. Someone using this is
standing in a car park on a hot Saturday deciding to go into the office; the whole
interaction should be one tap and a glance at the result.

Consequences of that:

* Buttons are large fixed durations, not a number entry or a slider. Nobody wants
  to type "180" on a phone.
* Current state is stated in words ("Cooling now, 2h 47m left"), not as a
  dashboard of point values.
* No build step, no framework, no external assets -- it has to load instantly over
  a phone connection and keep working when the office wifi is flaky.

The bypass timer is what makes this safe to expose to tenants without an approval
workflow: it expires on the device by itself, so the worst case of a stray tap is
a few hours of conditioning, not a floor left running all weekend.
"""

from __future__ import annotations

# Offered durations. Kept short and legible; the device caps at 1080 minutes but
# a tenant popping in on a weekend does not need an 18-hour option.
DURATIONS = ((60, "1 hour"), (120, "2 hours"), (180, "3 hours"), (240, "4 hours"))


def render_login(error: str = "", next_url: str = "/") -> str:
    """Login form, styled to match the tenant page since that is where it appears."""
    message = f'<p id="msg" class="err" style="display:block">{error}</p>' if error else ""
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Sign in</title>
<style>
 *{{box-sizing:border-box}}
 body{{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:env(safe-area-inset-top) 1.25rem 2rem;max-width:26rem;
      margin-inline:auto;background:#fff;color:#111}}
 h1{{font-size:1.35rem;margin:2rem 0 1.5rem}}
 label{{display:block;font-size:.9rem;color:#555;margin:0 0 .35rem}}
 input{{font:inherit;width:100%;padding:.9rem 1rem;border:1px solid #d0d0d5;
        border-radius:12px;margin-bottom:1rem;background:#fff;color:#111}}
 button{{font:inherit;font-weight:600;width:100%;padding:1.1rem;border:0;
         border-radius:14px;background:#1a73e8;color:#fff;min-height:56px}}
 #msg{{padding:.85rem 1rem;border-radius:12px;font-size:.95rem;display:none}}
 #msg.err{{background:#fce8e6;color:#c5221f}}
 @media(prefers-color-scheme:dark){{
   body{{background:#0d0d0f;color:#f2f2f4}} label{{color:#9a9aa2}}
   input{{background:#1c1c20;border-color:#3a3a42;color:#f2f2f4}}
   #msg.err{{background:#2d1614;color:#f28b82}}
 }}
</style>
<h1>Sign in</h1>
{message}
<form method="post" action="/login">
  <input type="hidden" name="next" value="{next_url}">
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autocapitalize="none"
         autocorrect="off" required autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>
"""


def render(device_id: int, name: str, state: dict) -> str:
    values = state.get("values", {})
    online = state.get("online", False)
    stale = state.get("stale", True)

    temp = values.get("space_temp")
    remaining = values.get("bypass_remaining_minutes")
    active = bool(values.get("bypass_active"))
    occupancy = values.get("effective_occupancy")

    temp_text = f"{temp:.0f}°F" if isinstance(temp, (int, float)) else "—"

    if not online or stale:
        status_class, status = "warn", "Can't reach the thermostat right now"
    elif active and isinstance(remaining, (int, float)) and remaining > 0:
        hours, minutes = divmod(int(remaining), 60)
        left = f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"
        status_class, status = "on", f"Running now — {left} left"
    elif occupancy == 1:  # OccupancyState.OCCUPIED
        status_class, status = "on", "Already occupied on the normal schedule"
    else:
        status_class, status = "off", "Currently unoccupied"

    buttons = "".join(
        f'<button type="button" data-minutes="{minutes}">{label}</button>'
        for minutes, label in DURATIONS
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111">
<title>{name} — comfort</title>
<style>
 *{{box-sizing:border-box}}
 body{{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:env(safe-area-inset-top) 1.25rem 2rem;max-width:30rem;
      margin-inline:auto;background:#fff;color:#111}}
 h1{{font-size:1.35rem;margin:1.5rem 0 .25rem}}
 .sub{{color:#666;margin:0 0 1.5rem}}
 .card{{border-radius:16px;padding:1.25rem;margin-bottom:1.5rem;background:#f4f4f5}}
 .temp{{font-size:3rem;font-weight:600;line-height:1;margin:0}}
 .status{{margin:.75rem 0 0;font-weight:500}}
 .on .status{{color:#137333}} .off .status{{color:#666}} .warn .status{{color:#c5221f}}
 h2{{font-size:1rem;margin:0 0 .75rem;color:#444}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}}
 button{{font:inherit;font-weight:600;padding:1.1rem .5rem;border-radius:14px;
         border:1px solid #d0d0d5;background:#fff;color:#111;cursor:pointer;
         -webkit-tap-highlight-color:transparent;min-height:56px}}
 button:active{{transform:scale(.97)}}
 button[disabled]{{opacity:.5}}
 .stop{{grid-column:1/-1;border-color:#f0c0bd;color:#c5221f;margin-top:.25rem}}
 #msg{{margin-top:1rem;padding:.85rem 1rem;border-radius:12px;display:none;font-size:.95rem}}
 #msg.ok{{display:block;background:#e6f4ea;color:#137333}}
 #msg.err{{display:block;background:#fce8e6;color:#c5221f}}
 @media(prefers-color-scheme:dark){{
   body{{background:#0d0d0f;color:#f2f2f4}} .card{{background:#1c1c20}}
   .sub,h2,.off .status{{color:#9a9aa2}}
   button{{background:#26262b;border-color:#3a3a42;color:#f2f2f4}}
   .on .status{{color:#81c995}} .warn .status{{color:#f28b82}}
   .stop{{border-color:#5c2f2c;color:#f28b82}}
   #msg.ok{{background:#122b1a;color:#81c995}} #msg.err{{background:#2d1614;color:#f28b82}}
 }}
</style>

<h1>{name}</h1>
<p class="sub">Heading in? Start the air early.</p>

<div class="card {status_class}">
  <p class="temp">{temp_text}</p>
  <p class="status">{status}</p>
</div>

<h2>Start conditioning for</h2>
<div class="grid">
  {buttons}
  <button type="button" class="stop" data-minutes="0">Stop early</button>
</div>

<p id="msg"></p>

<script>
const msg = document.getElementById('msg');
const buttons = [...document.querySelectorAll('button')];

async function send(minutes) {{
  buttons.forEach(b => b.disabled = true);
  msg.className = ''; msg.textContent = '';
  try {{
    const res = await fetch('/devices/{device_id}/bypass', {{
      method: 'POST',
      headers: {{'content-type': 'application/json'}},
      body: JSON.stringify({{minutes}})
    }});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    msg.className = 'ok';
    msg.textContent = minutes
      ? 'On its way. The office will be comfortable shortly.'
      : 'Stopped. Back to the normal schedule.';
    // The device is polled, so give it a cycle before showing the new state.
    setTimeout(() => location.reload(), 2500);
  }} catch (err) {{
    // Never claim failure outright: a write can be applied even when the
    // acknowledgement does not come back.
    msg.className = 'err';
    msg.textContent = 'Could not confirm that. Pull down to refresh and check '
                    + 'the status above before trying again.';
    buttons.forEach(b => b.disabled = false);
  }}
}}

buttons.forEach(b => b.addEventListener('click', () => send(+b.dataset.minutes)));
</script>
"""
