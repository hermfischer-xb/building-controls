"""Operator interface pages.

Each function returns a complete HTML page. They read from the poll cache and the
store directly rather than calling the API over HTTP -- the data is already in
process -- but every *mutation* goes back through the JSON API from the browser,
so authorisation is enforced in exactly one place.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..holidays import describe, occurrences
from ..points import SETPOINT_LIMITS, OccupancyState, ScheduleState, TempMode
from ..schedules import DAYS, week_summary
from .layout import activity, chip, e, icon, num, page

DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

OCC_LABEL = {
    OccupancyState.OCCUPIED: ("Occupied", "ok"),
    OccupancyState.UNOCCUPIED: ("Unoccupied", ""),
    OccupancyState.BYPASS: ("Bypass", "warn"),
    OccupancyState.STANDBY: ("Standby", ""),
    OccupancyState.NO_OVERRIDE: ("No override", ""),
}

MODE_LABEL = {
    int(TempMode.COOL): "Cool", int(TempMode.REHEAT): "Reheat",
    int(TempMode.HEAT): "Heat", int(TempMode.EMERGENCY_HEAT): "Emergency heat",
    int(TempMode.OFF): "Off",
}

SCHED_LABEL = {
    int(ScheduleState.OCCUPIED): "Occupied",
    int(ScheduleState.UNOCCUPIED): "Unoccupied",
    int(ScheduleState.STANDBY): "Standby",
}


def _occ_chip(value: Any) -> str:
    try:
        label, kind = OCC_LABEL[OccupancyState(int(value))]
    except (ValueError, KeyError, TypeError):
        return chip("—")
    return chip(label, kind)


def _status_chip(device: dict) -> str:
    if not device.get("online"):
        return chip("offline", "bad")
    if device.get("stale"):
        return chip("stale", "warn")
    return chip("online", "ok")


# --- dashboard ---------------------------------------------------------------


def dashboard(user, devices: list[dict], reconcile: dict | None) -> str:
    if not devices:
        body = '<h1>Dashboard</h1><p class="empty">No devices visible for your account.</p>'
        return page("Dashboard", user, body, active="/")

    rows = []
    for d in devices:
        v = d.get("values", {})
        bypass = ""
        if v.get("bypass_active") and isinstance(v.get("bypass_remaining_minutes"), (int, float)):
            mins = int(v["bypass_remaining_minutes"])
            h, m = divmod(mins, 60)
            bypass = chip(f"bypass {h}h {m:02d}m" if h else f"bypass {m}m", "warn")
        rows.append(
            f"""<tr>
 <td><a href="/ui/devices/{d['device_id']}">{e(d['name'])}</a>
     <div class="sub">{e(d['zone'])} · {e(d['address'])}</div></td>
 <td>{_status_chip(d)} {bypass}</td>
 <td class="num big" style="font-size:1.25rem">{num(v.get('space_temp'), '°')}</td>
 <td class="num">{num(v.get('effective_heat_sp'), '', 0)} / {num(v.get('effective_cool_sp'), '', 0)}</td>
 <td>{activity(v)}</td>
 <td>{_occ_chip(v.get('effective_occupancy'))}</td>
 <td class="num sub">{num(d.get('age_seconds'), 's', 0)}</td>
</tr>"""
        )

    online = sum(1 for d in devices if d.get("online"))
    offline = [d for d in devices if not d.get("online")]

    banner = ""
    if offline:
        names = ", ".join(e(d["name"]) for d in offline[:5])
        more = f" and {len(offline) - 5} more" if len(offline) > 5 else ""
        banner = (
            f'<div class="card" style="border-left:3px solid var(--bad)">'
            f"<strong>{len(offline)} device(s) unreachable:</strong> {names}{more}</div>"
        )

    drift_note = ""
    if reconcile and user.at_least("manager"):
        drifts = [abs(v) for v in (reconcile.get("clock_drift_seconds") or {}).values()]
        worst = max(drifts) if drifts else None
        age = reconcile.get("age_seconds")
        parts = []
        if age is not None:
            parts.append(f"reconciled {int(age)}s ago")
        if worst is not None:
            parts.append(f"worst clock drift {worst:.0f}s")
        if parts:
            drift_note = f'<p class="sub">{e(" · ".join(parts))} · <a href="/ui/system">System</a></p>'

    body = f"""
<h1>Dashboard</h1>
<p class="lede">{online} of {len(devices)} device(s) responding.</p>
{banner}
<div class="card"><div class="wrap"><table>
 <tr><th>Device</th><th>Status</th><th class="num">Space</th>
     <th class="num">Heat / Cool</th><th>Activity</th><th>Occupancy</th>
     <th class="num">Polled</th></tr>
 {''.join(rows)}
</table></div></div>
{drift_note}
<script>
// The devices come from a poll cache, so refreshing the page is the honest way
// to show new values; a partial DOM update would imply more liveness than exists.
setTimeout(() => location.reload(), 15000);
</script>
"""
    return page("Dashboard", user, body, active="/")


# --- device detail -----------------------------------------------------------


def device_detail(
    user, device: dict, groups: list[dict], group_id: int | None,
    overrides: dict[int, list[dict]], weekly: dict[str, list[dict]] | None,
    known_zones: list[str] | None = None,
) -> str:
    v = device.get("values", {})
    is_manager = user.at_least("manager")
    did = device["device_id"]

    setpoint_rows = ""
    if is_manager:
        fields = [
            ("occ_heat_sp", "Occupied heat"), ("occ_cool_sp", "Occupied cool"),
            ("standby_heat_sp", "Standby heat"), ("standby_cool_sp", "Standby cool"),
            ("unocc_heat_sp", "Unoccupied heat"), ("unocc_cool_sp", "Unoccupied cool"),
        ]
        inputs = []
        for key, label in fields:
            lo, hi = SETPOINT_LIMITS.get(key, (40, 99))
            val = v.get(key)
            inputs.append(
                f"""<div>
 <label for="{key}">{e(label)} <span class="sub">({lo:.0f}–{hi:.0f})</span></label>
 <div class="row">
  <input id="{key}" type="number" step="1" min="{lo:.0f}" max="{hi:.0f}"
         value="{num(val, '', 0)}">
  <button onclick="act('POST','/devices/{did}/points/{key}',
          {{value:+document.getElementById('{key}').value}},'{e(label)} written')">Set</button>
 </div></div>"""
            )
        setpoint_rows = f"""
<h2>Setpoints</h2>
<div class="card"><div class="grid three">{''.join(inputs)}</div>
<p class="sub">Values outside the range are clamped, not rejected. Writes go to the
device immediately; the schedule still decides which pair is in effect.</p></div>"""

    # Bypass is available to every role -- it is self-expiring, which is what makes
    # it safe to leave in a tenant's hands.
    bypass_state = ""
    if v.get("bypass_active") and isinstance(v.get("bypass_remaining_minutes"), (int, float)):
        mins = int(v["bypass_remaining_minutes"])
        h, m = divmod(mins, 60)
        # "about", not "left": no_BypassRemTime reports the configured period and
        # was not observed decrementing, so a countdown would be a claim the
        # device does not support.
        period = f"{h}h {m:02d}m" if h else f"{m} min"
        bypass_state = f'<p>{chip(f"running · about {period}", "warn")}</p>'

    bypass_buttons = "".join(
        f"""<button onclick="act('POST','/devices/{did}/bypass',{{minutes:{m}}},'Bypass {label} started')">{label}</button>"""
        for m, label in ((60, "1 hour"), (120, "2 hours"), (180, "3 hours"), (240, "4 hours"))
    )

    override_block = ""
    if is_manager:
        opts = "".join(
            f'<option value="{s.name}">{e(OCC_LABEL[s][0])}</option>' for s in OccupancyState
        )
        override_block = f"""
<h2>Occupancy override</h2>
<div class="card">
 <p class="sub">Unlike bypass this does <strong>not</strong> expire. Set it back to
    “No override” to return control to the schedule.</p>
 <div class="row">
  <select id="ovr">{opts}</select>
  <button onclick="act('POST','/devices/{did}/override',
          {{state:document.getElementById('ovr').value}},'Override set')">Apply</button>
 </div>
 <p class="sub">Currently {_occ_chip(v.get('occupancy_override'))}</p>
</div>"""

    schedule_block = ""
    if weekly:
        rows = []
        for i, day in enumerate(DAYS):
            transitions = weekly.get(day, [])
            overridden = (i + 1) in overrides
            if transitions:
                spans = " → ".join(
                    f"{e(t['time'])} {e(SCHED_LABEL.get(t['state'], t['state']))}"
                    for t in transitions
                )
            else:
                spans = '<span class="sub">closed</span>'
            rows.append(
                f"<tr><td>{DAY_LABELS[i]}"
                + (f' {chip("override", "warn")}' if overridden else "")
                + f"</td><td>{spans}</td></tr>"
            )

        assign = ""
        if is_manager and groups:
            opts = "".join(
                f'<option value="{g["id"]}"{" selected" if g["id"] == group_id else ""}>'
                f"{e(g['name'])}</option>"
                for g in groups
            )
            assign = f"""
 <div class="row" style="margin-top:1rem">
  <div><label for="grp">Schedule group</label><select id="grp">{opts}</select></div>
  <button onclick="act('PUT','/devices/{did}/schedule-group/'+document.getElementById('grp').value,
          null,'Group assigned — reconcile to apply')">Assign</button>
  <button class="primary" onclick="act('POST','/reconcile',null,'Reconciled')">Reconcile now</button>
 </div>"""

        current = "the device's own schedule" if group_id is None else "assigned group"
        schedule_block = f"""
<h2>Weekly schedule</h2>
<div class="card">
 <p class="sub">Read from the thermostat. Source: {e(current)}.</p>
 <table>{''.join(rows)}</table>
 {assign}
</div>"""

    # Moving a device between zones is how a tenant relocation is recorded: it
    # changes which tenants can reach it and which zone-scoped holidays apply.
    zone_block = ""
    if is_manager:
        opts = "".join(
            f'<option value="{e(z)}"{" selected" if z == device["zone"] else ""}>{e(z)}</option>'
            for z in (known_zones or [])
        )
        zone_block = f"""
<h2>Zone</h2>
<div class="card">
 <p class="sub">Which tenants can reach this thermostat, and which zone-scoped
    holidays apply to it. Change this when a tenant moves office.</p>
 <div class="row">
  <div><label for="zsel">Existing zone</label><select id="zsel">{opts}</select></div>
  <div><label for="znew">or a new one</label><input id="znew" placeholder="suite-410"></div>
  <button onclick="saveZone({did})">Move</button>
 </div>
</div>
<script>
function saveZone(id){{
  const typed = document.getElementById('znew').value.trim();
  const zone = typed || document.getElementById('zsel').value;
  if (!zone) {{ toast('Pick or type a zone', false); return; }}
  act('PUT', '/devices/' + id + '/zone', {{zone}}, 'Moved to ' + zone);
}}
</script>"""

    body = f"""
<h1>{e(device['name'])}</h1>
<p class="lede">Device {did} · {chip(device['zone'])} · {e(device['address'])} {_status_chip(device)}</p>

<div class="grid two">
 <div class="card">
  <div class="sub">Space temperature</div>
  <div class="big">{num(v.get('space_temp'), '°F')}</div>
  <div class="sub">humidity {num(v.get('space_humidity'), '%')}</div>
 </div>
 <div class="card">
  <div class="sub">Effective</div>
  <div class="big">{num(v.get('effective_heat_sp'), '', 0)} / {num(v.get('effective_cool_sp'), '', 0)}</div>
  <div class="sub">{_occ_chip(v.get('effective_occupancy'))}
      schedule says {e(SCHED_LABEL.get(v.get('schedule_state'), '—'))}</div>
 </div>
</div>

<div class="card">
 <div class="sub">Equipment</div>
 <div style="margin:.4rem 0 .2rem;font-size:1.05rem">{activity(v)}</div>
 <div class="sub">mode {e(MODE_LABEL.get(v.get('temp_mode'), '—'))} ·
   {num(v.get('active_heat_stages'), '', 0)} heat /
   {num(v.get('active_cool_stages'), '', 0)} cool stage(s) active</div>
</div>

<h2>Start conditioning now</h2>
<div class="card">
 {bypass_state}
 <div class="row">{bypass_buttons}
  <button class="danger" onclick="act('POST','/devices/{did}/bypass',{{minutes:0}},'Bypass stopped')">Stop</button>
 </div>
 <p class="sub">Bypass expires on the device by itself.</p>
</div>
{override_block}
{setpoint_rows}
{schedule_block}
{zone_block}
<p style="margin-top:2rem"><a href="/">← All devices</a></p>
"""
    return page(device["name"], user, body, active="/")


# --- schedules ---------------------------------------------------------------


def schedules(user, groups: list[dict], assignments: list[dict]) -> str:
    cards = []
    for g in groups:
        rows = []
        for i, label in enumerate(DAY_LABELS, start=1):
            transitions = g["week"].get(i) or g["week"].get(str(i)) or []
            value = ",".join(f"{t['time']}={t['state']}" for t in transitions)
            rows.append(
                f"""<tr>
 <td style="width:3.5rem">{label}</td>
 <td><input id="g{g['id']}d{i}" value="{e(value)}" style="width:100%"></td>
 <td><button onclick="saveDay({g['id']},{i})">Save</button></td>
</tr>"""
            )
        used = [a["name"] for a in assignments if a["group_id"] == g["id"]]
        cards.append(
            f"""<div class="card">
 <h2 style="margin-top:0">{e(g['name'])}</h2>
 <p class="sub">{e(g.get('description') or '')}</p>
 <p class="sub">{e(week_summary({int(k): v for k, v in g['week'].items()}))}</p>
 <table>{''.join(rows)}</table>
 <p class="sub" style="margin-top:.75rem">Used by:
    {e(', '.join(used)) if used else 'no devices'}</p>
</div>"""
        )

    body = f"""
<h1>Schedule groups</h1>
<p class="lede">A group is a normal week. Devices inherit one, and may override
individual days — so a change here still reaches every day a tenant has not
specifically customised.</p>

<div class="card">
 <strong>Editing format</strong>
 <p class="sub" style="margin:.35rem 0 0">
  Comma-separated <code>HH:MM=state</code>, in time order.
  State is <code>0</code> occupied, <code>1</code> unoccupied, <code>3</code> standby.<br>
  Example: <code>06:00=0,18:00=1</code> — occupied 6am, unoccupied 6pm.<br>
  A closed day is <code>00:00=1</code>. The device accepts up to 8 transitions,
  but its own touchscreen only displays about 4.
 </p>
</div>

{''.join(cards) if cards else '<p class="empty">No groups yet.</p>'}

<div class="card">
 <button class="primary" onclick="act('POST','/groups/seed-defaults',null,'Default groups added')">
   Add default groups</button>
 <button onclick="act('POST','/reconcile',null,'Reconciled')">Reconcile now</button>
 <p class="sub">Edits are stored as intent. Nothing reaches a thermostat until a
    reconcile runs — automatically every few minutes, or immediately here.</p>
</div>

<script>
function parseDay(text){{
  const out = [];
  for (const part of text.split(',').map(s => s.trim()).filter(Boolean)) {{
    const [time, state] = part.split('=');
    if (!time || state === undefined) throw new Error('Use HH:MM=state, e.g. 06:00=0');
    out.push({{time: time.trim(), state: parseInt(state, 10)}});
  }}
  if (!out.length) throw new Error('A day needs at least one transition (use 00:00=1 for closed)');
  return out;
}}
async function saveDay(groupId, day){{
  const field = document.getElementById('g' + groupId + 'd' + day);
  let transitions;
  try {{ transitions = parseDay(field.value); }}
  catch (err) {{ toast(err.message, false); return; }}
  await act('PUT', '/groups/' + groupId + '/days/' + day, {{transitions}},
            'Saved — reconcile to apply');
}}
</script>
"""
    return page("Schedules", user, body, active="/ui/schedules")


# --- holidays and exceptions -------------------------------------------------


def holidays(user, rules: list[dict], exceptions: list[dict], year: int,
             known_zones: list[str] | None = None,
             devices: list[dict] | None = None) -> str:
    zone_opts = "".join(f'<option value="{e(z)}">{e(z)}</option>' for z in (known_zones or []))
    device_opts = "".join(
        f'<option value="{d["device_id"]}">{e(d["name"])}</option>' for d in (devices or [])
    )

    rows = []
    for h in rules:
        dates = h.get("dates") or []
        when = e(dates[0]) if len(dates) == 1 else (
            f"{e(dates[0])} … {e(dates[-1])}" if dates else '<span class="sub">—</span>'
        )
        scope_chip = (
            chip("everywhere") if h.get("scope", "global") == "global"
            else chip(f"{h['scope']}: {h['scope_ref']}")
        )
        # The dates this rule resolves to, so "working through it" can target the
        # right day without the manager working out when a floating holiday falls.
        date_opts = "".join(f'<option value="{e(d)}">{e(d)}</option>' for d in dates)
        hid = h["id"]

        rows.append(
            f"""<tr>
 <td>{e(h['name'])}</td>
 <td>{chip(h['rule_type'])}</td>
 <td>{when}</td>
 <td>{e(SCHED_LABEL.get(h['state'], h['state']))}</td>
 <td>{scope_chip}</td>
 <td><div class="row" style="gap:.35rem">
   <button onclick="toggleWork({hid})" title="A tenant is working this day">Working…</button>
   <button class="danger" onclick="act('DELETE','/holidays/{hid}',null,'Removed')">Remove</button>
 </div></td>
</tr>
<tr id="wt{hid}" style="display:none"><td colspan="6">
 <div class="card" style="margin:.35rem 0">
  <p class="sub"><strong>{e(h['name'])}</strong> — a tenant is working this day.
     Creates a dated exception for their zone, which the thermostat applies
     <em>over</em> the holiday. The holiday itself is left alone for everyone else.</p>
  <div class="row">
   <div><label>Zone</label><select id="wtz{hid}">{zone_opts}</select></div>
   <div><label>Date</label><select id="wtd{hid}">{date_opts}</select></div>
   <div><label>Occupied from</label><input id="wtf{hid}" type="time" value="08:00"></div>
   <div><label>until</label><input id="wtu{hid}" type="time" value="17:00"></div>
   <button class="primary" onclick="saveWork({hid},'{e(h['name'])}')">Save</button>
  </div>
 </div>
</td></tr>"""
        )

    ex_rows = []
    for x in exceptions:
        span = e(x["start_date"]) + (f" … {e(x['end_date'])}" if x.get("end_date") else "")
        times = ", ".join(
            f"{e(t['time'])} {e(SCHED_LABEL.get(t['state'], t['state']))}"
            for t in x["transitions"]
        )
        ex_rows.append(
            f"""<tr><td>{e(x['name'])}</td><td>{span}</td><td>{times}</td>
 <td>{e(x['scope'])}:{e(x['scope_ref'])}</td>
 <td><button class="danger" onclick="act('DELETE','/exceptions/{x['id']}',null,'Removed')">Remove</button></td></tr>"""
        )

    body = f"""
<h1>Holidays</h1>
<p class="lede">Stored as <em>rules</em>, not dates. Floating rules are evaluated by
the thermostat itself, so they never need re-entering — dates shown are for {year}.</p>

<div class="card"><div class="wrap"><table>
 <tr><th>Name</th><th>Type</th><th>{year}</th><th>State</th><th>Zone</th><th></th></tr>
 {''.join(rows) if rows else '<tr><td colspan="6" class="empty">No holidays yet.</td></tr>'}
</table></div>
<div class="row" style="margin-top:1rem">
 <button onclick="act('POST','/holidays/seed-us-federal',null,'US federal holidays added')">
   Add US federal holidays</button>
 <button class="primary" onclick="act('POST','/reconcile',null,'Reconciled')">Reconcile now</button>
</div></div>

<h2>Add a holiday</h2>
<div class="card">
 <div class="row">
  <div><label for="hname">Name</label><input id="hname" placeholder="Company day"></div>
  <div><label for="htype">Type</label>
   <select id="htype" onchange="showFields()">
    <option value="fixed">Same date each year</option>
    <option value="floating">Floating (nth weekday)</option>
    <option value="range">Date range</option>
   </select></div>
  <div><label for="hstate">State</label>
   <select id="hstate"><option value="1">Unoccupied</option><option value="3">Standby</option></select></div>
  <div><label for="hscope">Applies to</label>
   <select id="hscope" onchange="showScope()">
    <option value="global">The whole building</option>
    <option value="zone">One zone</option>
    <option value="device">One thermostat</option>
   </select></div>
  <div id="sc-zone" style="display:none"><label for="hzone">Zone</label>
   <select id="hzone">{zone_opts}</select></div>
  <div id="sc-dev" style="display:none"><label for="hdev">Thermostat</label>
   <select id="hdev">{device_opts}</select></div>
 </div>
 <div class="row" id="f-fixed" style="margin-top:.75rem">
  <div><label for="hmonth">Month</label><input id="hmonth" type="number" min="1" max="12" value="12"></div>
  <div><label for="hday">Day</label><input id="hday" type="number" min="1" max="31" value="24"></div>
 </div>
 <div class="row" id="f-float" style="margin-top:.75rem;display:none">
  <div><label for="fmonth">Month</label><input id="fmonth" type="number" min="1" max="12" value="11"></div>
  <div><label for="fweek">Week</label>
   <select id="fweek"><option value="1">1st</option><option value="2">2nd</option>
   <option value="3">3rd</option><option value="4">4th</option><option value="5">last</option></select></div>
  <div><label for="fdow">Weekday</label>
   <select id="fdow">{''.join(f'<option value="{i}">{d}</option>' for i, d in enumerate(DAY_LABELS, 1))}</select></div>
 </div>
 <div class="row" id="f-range" style="margin-top:.75rem;display:none">
  <div><label>From</label><div class="row">
   <input id="rmonth" type="number" min="1" max="12" value="12" title="month">
   <input id="rday" type="number" min="1" max="31" value="24" title="day"></div></div>
  <div><label>To</label><div class="row">
   <input id="remonth" type="number" min="1" max="12" value="12" title="month">
   <input id="reday" type="number" min="1" max="31" value="26" title="day"></div></div>
 </div>
 <div class="row" style="margin-top:1rem">
  <button class="primary" onclick="addHoliday()">Add holiday</button>
 </div>
</div>

<h2>One-off dates</h2>
<p class="lede">A specific date the building is open or closed outside its normal
pattern. Overrides holidays on the device.</p>
<div class="card"><div class="wrap"><table>
 <tr><th>Name</th><th>Dates</th><th>Times</th><th>Scope</th><th></th></tr>
 {''.join(ex_rows) if ex_rows else '<tr><td colspan="5" class="empty">None scheduled.</td></tr>'}
</table></div>
<div class="row" style="margin-top:1rem">
 <div><label for="xname">Name</label><input id="xname" placeholder="Saturday working"></div>
 <div><label for="xdate">Date</label><input id="xdate" type="date"></div>
 <div><label for="xfrom">Occupied from</label><input id="xfrom" type="time" value="08:00"></div>
 <div><label for="xto">until</label><input id="xto" type="time" value="13:00"></div>
 <button class="primary" onclick="addException()">Add</button>
</div></div>

<script>
function showFields(){{
  const t = document.getElementById('htype').value;
  document.getElementById('f-fixed').style.display = t === 'fixed' ? 'flex' : 'none';
  document.getElementById('f-float').style.display = t === 'floating' ? 'flex' : 'none';
  document.getElementById('f-range').style.display = t === 'range' ? 'flex' : 'none';
}}
const val = id => +document.getElementById(id).value;
function toggleWork(id){{
  const row = document.getElementById('wt' + id);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}}
function saveWork(id, name){{
  const zone = document.getElementById('wtz' + id).value;
  const date = document.getElementById('wtd' + id).value;
  if (!zone || !date) {{ toast('Pick a zone and a date', false); return; }}
  act('POST', '/exceptions', {{
    name: name + ' — ' + zone + ' working',
    start_date: date,
    transitions: [{{time: document.getElementById('wtf' + id).value, state: 0}},
                  {{time: document.getElementById('wtu' + id).value, state: 1}}],
    scope: 'zone', scope_ref: zone
  }}, 'Recorded — reconcile to apply');
}}
function showScope(){{
  const sc = document.getElementById('hscope').value;
  document.getElementById('sc-zone').style.display = sc === 'zone' ? 'block' : 'none';
  document.getElementById('sc-dev').style.display  = sc === 'device' ? 'block' : 'none';
}}
function addHoliday(){{
  const type = document.getElementById('htype').value;
  const scope = document.getElementById('hscope').value;
  const body = {{name: document.getElementById('hname').value.trim(),
                rule_type: type, state: val('hstate'), scope,
                scope_ref: scope === 'zone' ? document.getElementById('hzone').value
                         : scope === 'device' ? document.getElementById('hdev').value
                         : '*'}};
  if (!body.name) {{ toast('Give it a name', false); return; }}
  if (type === 'fixed')    {{ body.month = val('hmonth'); body.day = val('hday'); }}
  if (type === 'floating') {{ body.month = val('fmonth');
                             body.week_of_month = val('fweek'); body.day_of_week = val('fdow'); }}
  if (type === 'range')    {{ body.month = val('rmonth'); body.day = val('rday');
                             body.end_month = val('remonth'); body.end_day = val('reday'); }}
  act('POST', '/holidays', body, 'Added — reconcile to apply');
}}
function addException(){{
  const name = document.getElementById('xname').value.trim();
  const date = document.getElementById('xdate').value;
  if (!name || !date) {{ toast('Name and date are required', false); return; }}
  act('POST', '/exceptions', {{
    name, start_date: date,
    transitions: [{{time: document.getElementById('xfrom').value, state: 0}},
                  {{time: document.getElementById('xto').value, state: 1}}]
  }}, 'Added — reconcile to apply');
}}
</script>
"""
    return page("Holidays", user, body, active="/ui/holidays")


# --- system ------------------------------------------------------------------


def system(user, health: dict, reconcile: dict, audit: list[dict]) -> str:
    drift = reconcile.get("clock_drift_seconds") or {}
    drift_rows = "".join(
        f"<tr><td>{e(k)}</td><td class='num'>{float(v):+.1f}s</td>"
        f"<td>{chip('ok','ok') if abs(float(v)) <= 30 else chip('drifting','warn')}</td></tr>"
        for k, v in sorted(drift.items())
    )

    dev_rows = []
    for d in reconcile.get("devices", []):
        state = chip("ok", "ok") if d.get("ok") else chip("errors", "bad")
        detail = "; ".join(d.get("errors") or d.get("changed") or []) or "no changes"
        dev_rows.append(
            f"<tr><td>{e(d['name'])}</td><td>{state}</td><td class='sub'>{e(detail)}</td></tr>"
        )

    audit_rows = []
    for a in audit:
        when = dt.datetime.fromtimestamp(a["ts"]).strftime("%d %b %H:%M:%S")
        kind = "" if a["outcome"] == "ok" else ("warn" if a["outcome"] == "unknown" else "bad")
        audit_rows.append(
            f"""<tr><td class="sub">{e(when)}</td><td>{e(a['actor'])}</td>
 <td>{e(a['action'])}</td><td class="sub">{e(a['target'] or '')}</td>
 <td>{chip(a['outcome'], kind) if kind else ''}</td></tr>"""
        )

    age = reconcile.get("age_seconds")
    body = f"""
<h1>System</h1>
<p class="lede">{e(health.get('devices_online'))} of {e(health.get('devices_total'))}
devices responding · polling every {e(health.get('poll_interval_seconds'))}s
· last reconcile {e(f"{int(age)}s ago" if age is not None else "not yet run")}</p>

<div class="card">
 <button class="primary" onclick="act('POST','/reconcile',null,'Reconciled')">Reconcile now</button>
 <span class="sub">Pushes stored intent — schedules, holidays, setpoints, clock — to every device.</span>
</div>

<h2>Last reconcile</h2>
<div class="card"><table>
 {''.join(dev_rows) if dev_rows else '<tr><td class="empty">Not yet run.</td></tr>'}
</table></div>

<h2>Clock drift</h2>
<div class="card">
 <p class="sub">The thermostats have no time source on an isolated network, so this
    host is their clock. Drift is corrected automatically past the configured limit.</p>
 <table>{drift_rows or '<tr><td class="empty">No readings yet.</td></tr>'}</table>
</div>

<h2>Recent activity</h2>
<div class="card"><div class="wrap"><table>
 <tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th></th></tr>
 {''.join(audit_rows) if audit_rows else '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'}
</table></div></div>
"""
    return page("System", user, body, active="/ui/system")


# --- users -------------------------------------------------------------------


def users(user, accounts: list[dict], zones: list[str]) -> str:
    def zone_editor(a: dict) -> str:
        """Checkboxes of real zones rather than a text field.

        Free text needed a legend and a parser, and still let someone type
        "floor3" for "floor-3" -- a typo that silently grants access to nothing
        and is invisible until the tenant complains. Offering only zones that
        exist removes the failure mode instead of validating it after the fact.
        """
        if a["role"] != "tenant":
            return '<span class="sub">all zones — role is not zone-scoped</span>'
        if not zones:
            return '<span class="sub">no zones defined yet</span>'
        held = set(a["zones"])
        boxes = "".join(
            f'<label style="display:inline-flex;align-items:center;gap:.3rem;'
            f'margin:0 .7rem .3rem 0;color:inherit">'
            f'<input type="checkbox" name="z-{e(a["username"])}" value="{e(z)}"'
            f'{" checked" if z in held else ""}> {e(z)}</label>'
            for z in zones
        )
        return (
            f'<div>{boxes}</div>'
            f'<button style="margin-top:.35rem" '
            f'onclick="saveZones(\'{e(a["username"])}\')">Save zones</button>'
        )

    rows = "".join(
        f"""<tr>
 <td>{e(a['display_name'] or a['username'])}<div class="sub">{e(a['username'])}</div></td>
 <td>{chip(a['role'], 'ok' if a['active'] else 'bad')}</td>
 <td>{zone_editor(a)}</td>
 <td class="sub">{e(dt.datetime.fromtimestamp(a['last_login']).strftime('%d %b %H:%M')
                    if a.get('last_login') else 'never')}</td>
 <td>{'' if a['username'] == user.username or not a['active'] else
      f'''<button class="danger" onclick="act('DELETE','/users/{e(a['username'])}',null,'Deactivated')">Deactivate</button>'''}</td>
</tr>"""
        for a in accounts
    )

    zone_opts = "".join(f'<option value="{e(z)}">{e(z)}</option>' for z in zones)
    body = f"""
<h1>Users</h1>
<p class="lede">Tenants are scoped to zones and may only start conditioning in their
own suite. Managers run the building; admins also manage accounts.</p>

<div class="card"><div class="wrap"><table>
 <tr><th>Name</th><th>Role</th><th>Zones</th><th>Last seen</th><th></th></tr>
 {rows or '<tr><td colspan="5" class="empty">No accounts.</td></tr>'}
</table></div></div>

<h2>Add someone</h2>
<div class="card">
 <div class="row">
  <div><label for="uname">Username</label><input id="uname" autocapitalize="none"></div>
  <div><label for="udisp">Display name</label><input id="udisp"></div>
  <div><label for="urole">Role</label>
   <select id="urole"><option value="tenant">Tenant</option>
   <option value="manager">Manager</option><option value="admin">Admin</option></select></div>
  <div><label for="uzone">Zone (tenants)</label>
   <select id="uzone"><option value="">—</option>{zone_opts}</select></div>
  <div><label for="upass">Password</label><input id="upass" type="password"></div>
  <button class="primary" onclick="addUser()">Create</button>
 </div>
 <p class="sub">Minimum 8 characters. Passwords are hashed with scrypt; changing one
    revokes that account's sessions.</p>
</div>

<script>
function saveZones(username){{
  const zones = [...document.querySelectorAll(
      'input[name="z-' + username + '"]:checked')].map(b => b.value);
  act('PUT', '/users/' + encodeURIComponent(username) + '/zones', {{zones}},
      zones.length ? 'Zones updated' : 'All zones removed — tenant sees nothing');
}}
function addUser(){{
  const zone = document.getElementById('uzone').value;
  const body = {{
    username: document.getElementById('uname').value.trim(),
    display_name: document.getElementById('udisp').value.trim(),
    role: document.getElementById('urole').value,
    password: document.getElementById('upass').value,
    zones: zone ? [zone] : []
  }};
  if (!body.username || body.password.length < 8) {{
    toast('Username required, password at least 8 characters', false); return;
  }}
  act('POST', '/users', body, 'Account created');
}}
</script>
"""
    return page("Users", user, body, active="/ui/users")


# --- zones -------------------------------------------------------------------


def zones_page(user, mapping: list[dict], known: list[str], tenants: list[dict]) -> str:
    """Device-to-zone mapping in one place.

    Every device has exactly one zone by construction: the override table is keyed
    on device id so it can hold at most one, and a device with no override falls
    back to the zone it was commissioned into. There is no state where a device
    has none or several, so this page never has to reconcile a conflict.
    """
    by_zone: dict[str, list[dict]] = {z: [] for z in known}
    for d in mapping:
        by_zone.setdefault(d["zone"], []).append(d)

    tenants_by_zone: dict[str, list[str]] = {}
    for t in tenants:
        for z in t["zones"]:
            tenants_by_zone.setdefault(z, []).append(t["display_name"] or t["username"])

    cards = []
    for zone in sorted(by_zone):
        devices = by_zone[zone]
        options = "".join(
            f'<option value="{e(z)}"{" selected" if z == zone else ""}>{e(z)}</option>'
            for z in known
        )
        rows = "".join(
            f"""<tr>
 <td><a href="/ui/devices/{d['device_id']}">{e(d['name'])}</a>
     <div class="sub">device {d['device_id']} · {e(d['address'])}</div></td>
 <td><select id="mv{d['device_id']}">{options}</select></td>
 <td><button onclick="moveDevice({d['device_id']})">Move</button></td>
</tr>"""
            for d in sorted(devices, key=lambda x: x["name"])
        )
        who = tenants_by_zone.get(zone, [])
        cards.append(
            f"""<div class="card">
 <h2 style="margin-top:0">{e(zone)} {chip(f"{len(devices)} device(s)")}</h2>
 <p class="sub">Tenants with access: {e(', '.join(who)) if who else 'none'}</p>
 {'<table>' + rows + '</table>' if rows
  else '<p class="empty">No devices in this zone.</p>'}
</div>"""
        )

    orphans = [z for z in known if not by_zone.get(z)]
    note = (
        f'<p class="sub">{len(orphans)} zone(s) with no devices: '
        f'{e(", ".join(orphans))}. They stay listed so tenant grants survive a '
        f'temporary move.</p>' if orphans else ""
    )

    body = f"""
<h1>Zones</h1>
<p class="lede">A zone groups thermostats for tenant access and for zone-scoped
holidays. Moving a device here is how a tenant relocation is recorded — it takes
effect immediately, for both access and scheduling.</p>

<div class="card">
 <div class="row">
  <div><label for="newzone">Create a zone</label>
   <input id="newzone" placeholder="suite-410"></div>
  <div><label for="newzonedev">by moving this device into it</label>
   <select id="newzonedev">
    {''.join(f'<option value="{d["device_id"]}">{e(d["name"])}</option>' for d in mapping)}
   </select></div>
  <button class="primary" onclick="createZone()">Create</button>
 </div>
 <p class="sub">A zone exists because a device is in it, so creating one means
    moving a device. Lower case with hyphens keeps them easy to type.</p>
</div>

{''.join(cards)}
{note}

<script>
function moveDevice(id){{
  const zone = document.getElementById('mv' + id).value;
  act('PUT', '/devices/' + id + '/zone', {{zone}}, 'Moved to ' + zone);
}}
function createZone(){{
  const zone = document.getElementById('newzone').value.trim();
  const id = document.getElementById('newzonedev').value;
  if (!zone) {{ toast('Name the zone first', false); return; }}
  if (!/^[a-z0-9][a-z0-9 _-]*$/i.test(zone)) {{
    toast('Use letters, numbers, spaces, hyphens or underscores', false); return;
  }}
  act('PUT', '/devices/' + id + '/zone', {{zone}}, 'Created ' + zone);
}}
</script>
"""
    return page("Zones", user, body, active="/ui/zones")
