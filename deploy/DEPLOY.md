# Deploying to the on-premises controller

Written for a Mac mini sitting on the building network. Do these in order — the
last step depends on the one before it, and getting `secure_cookies` out of
sequence produces a login that silently fails.

---

## 1. Make macOS behave like an appliance

macOS defaults assume a desktop someone sits at. Four of them will take the
building offline if left alone, and they all have the same shape — a safety
default with no unattended path around it:

| Default | Where it bites |
|---|---|
| FileVault | below |
| Unattended updates | below |
| TCC on the home folder | step 2 — dictates where the repo lives |
| Local Network Privacy | step 3 — silently kills all BACnet traffic |

The last two do not announce themselves. Read those steps before you deviate
from them; each one cost this deploy an hour.

### FileVault — decide deliberately

If FileVault is on, an unattended reboot leaves the disk locked and **the machine
never rejoins the network**. No polling, no schedules, no remote access, until
someone physically logs in.

```bash
fdesetup status
```

Either turn it off, or accept that every reboot needs a person on site. This is
the single most common way a Mac-as-server fails, and it always happens at 3am.

### Never sleep, and come back after a power cut

```bash
sudo pmset -a sleep 0 disablesleep 1 autorestart 1 standby 0 powernap 0
pmset -g                 # verify
```

`autorestart 1` is the one that matters after a building power failure.

### Do not let it update itself unattended

System Settings → General → Software Update → Automatic Updates: turn off
automatic install of macOS updates. A major version upgrade that stops at a setup
assistant takes the building offline until someone clicks through it.

### Log directory

```bash
sudo mkdir -p /usr/local/var/log
sudo chown "$(whoami)" /usr/local/var/log
```

---

## 2. Install the application

```bash
cd /Users/Shared            # not ~, and never ~/Documents -- see below
git clone https://github.com/hermfischer-xb/building-controls.git
cd building-controls
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/devices.example.yaml config/devices.yaml
```

Three things are deliberately **not** in git and must be created here:
`config/devices.yaml`, `data/bms.db`, and `.venv`.

**Not under `~/Documents`, or any TCC-protected folder.** Desktop, Documents and
Downloads all require user consent to read. A LaunchDaemon has no GUI session in
which to raise that prompt, so it is denied and dies quietly — respawning every
20 seconds with nothing obvious in the log. `/Users/Shared` is outside TCC and
belongs to no one user, which is what an appliance wants. Granting Full Disk
Access to the real interpreter (not the venv symlink) also works, but it is a
larger grant than the job needs.

**The venv is not relocatable.** `.venv/bin/pip`, `uvicorn` and `activate` bake
the absolute path into their shebangs, so a repo that gets moved leaves them
pointing at nothing. Nothing in this deployment uses them — the daemon runs
`.venv/bin/python -m bms`, and `.venv/bin/python -m pip` works regardless — but
if you move the tree, rebuild: `rm -rf .venv && python3 -m venv .venv &&
.venv/bin/python -m pip install -r requirements.txt`.

Edit `config/devices.yaml`. The value that differs from a laptop:

```yaml
bacnet:
  address: <this machine's IP on the thermostat network>/24
```

```bash
ipconfig getifaddr en0        # or whichever interface faces the thermostats
```

**Pin it explicitly.** If this host ends up on both the control network and the
office LAN, an unpinned bind picks one arbitrarily and Who-Is leaves by the wrong
interface.

**Discovery may need `--target`.** Broadcast Who-Is depends on the network
between the gateway and the devices carrying directed broadcast, and a Wi-Fi AP
bridging wireless thermostats to a wired host commonly drops it — the bare
broadcast form then returns nothing and looks exactly like a dead device:

```bash
.venv/bin/python tools/discover.py --address 192.168.144.1/24 --target 192.168.144.226
```

This affects commissioning only. The gateway itself never broadcasts; it reads
each device unicast by its configured address (`bms/bacnet.py`), and the device
inventory is config, not discovery (`bms/config.py`).

Create the first account, lock down the database, and check it runs in the
foreground before daemonising:

```bash
.venv/bin/python -m bms.useradmin add-admin herm     # blank prompt generates one
chmod 700 data && chmod 600 data/bms.db
.venv/bin/python -m bms --config config/devices.yaml
```

`/Users/Shared` is world-readable (`drwxrwxrwt`) and the default file mode is
not; `bms.db` holds the admin password hash and every live session token, so any
local account could otherwise copy it.

Confirm it polls a thermostat, then Ctrl-C. **A healthy poll logs nothing at
all** — the poll loop has no logging statements, by design. Silence is success;
`GET /health` is the only real signal.

---

## 3. Run it as a LaunchDaemon

Edit the paths in the plist first — launchd has no shell, no `PATH` and no
notion of a home directory, so every one must be absolute. Then, **one line at a
time**, so you can see which fails:

```bash
sudo cp deploy/com.building-controls.gateway.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.building-controls.gateway.plist
sudo chmod 644       /Library/LaunchDaemons/com.building-controls.gateway.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.gateway.plist

sudo launchctl print system/com.building-controls.gateway | head -20
curl -sS localhost:8237/health
```

Use `bootstrap`, not `load -w`. `load` is deprecated on macOS 15 and reports
failure as `Load failed: 5: Input/output error`, which says nothing about the
cause. To stop or replace it: `sudo launchctl bootout
system/com.building-controls.gateway`.

Use `curl -sS`, not `-s`. Plain `-s` swallows the error too, so a failed check
looks identical to a check that printed nothing.

A **Daemon**, not an Agent — agents wait for a login, so after an unattended
reboot the building would have no controller until someone sat down at it.

### If it starts but no device ever answers

The signature is a daemon that reaches the internet fine while every BACnet read
times out — in one second of log, `did not answer during inventory check`
alongside a successful outbound HTTPS call — and the *same code, run by hand
from a terminal, working perfectly.*

That is macOS Sequoia's **Local Network Privacy**. Terminal holds a local-network
grant, which is why interactive testing succeeds; a LaunchDaemon has none and no
session in which to prompt, so its LAN traffic is dropped silently. Nothing is
wrong with the config, the venv, or the device.

System Settings → Privacy & Security → **Local Network**, enable `Python`, then:

```bash
sudo launchctl kickstart -k system/com.building-controls.gateway
```

The entry usually appears only *after* a process has attempted access, so if the
list looks empty, let the daemon run a poll cycle and look again. Running the
daemon as root is exempt from the restriction and is the fallback if the grant
cannot be made — but it discards the plist's reason for not being root, and a
process that talks to unauthenticated field devices is the wrong one to promote.
Take the grant if you can get it; it survives a reboot.

**Reboot the machine now and confirm it comes back on its own** — without
logging in, since that is what a 3am power cut looks like:

```bash
curl -sS localhost:8237/health      # want "devices_online" equal to your device count
```

This confirms two separate things: that the daemon starts unattended, and that
the Local Network grant survives a reboot for a process with no GUI session.
Better to find out here than after the first power cut.

---

## 4. Cloudflare Tunnel

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create building-controls
cloudflared tunnel route dns building-controls controls.16400ventura.com
```

Put `deploy/cloudflared-config.yml` at `/etc/cloudflared/config.yml` and edit the
TruPortal LAN address — **or, if TruPortal is deferred, comment that whole
ingress block out.** cloudflared validates ingress at startup and will not come
up with `CHANGEME-TRUPORTAL-LAN-IP` in the file. Skip its `tunnel route dns`
line too, so the hostname does not exist until there is something behind it;
steps 4 and 7 then cover `controls.16400ventura.com` only. Then:

```bash
sudo cloudflared service install
cloudflared tunnel info building-controls
```

Outbound only — no inbound port, no firewall change, and the public IP stops
being an attack surface.

Test <https://controls.16400ventura.com> before continuing.

---

## 5. Only now, tell the app it is behind a proxy

```yaml
secure_cookies: true
behind_proxy: true
client_ip_header: cf-connecting-ip
```

```bash
sudo launchctl kickstart -k system/com.building-controls.gateway
```

**Order matters.** `secure_cookies: true` on plain HTTP means the browser accepts
the session cookie and never sends it back — login appears to succeed and does
not stick, with nothing in the logs to explain it.

`behind_proxy` matters more than it looks. Behind a tunnel every request arrives
from `127.0.0.1`, so a login throttle keyed on that would put every failed login
on the internet into one bucket and let eight bad passwords lock out the whole
building. The client-IP header is trusted *only* when this is set.

Verify:

```bash
.venv/bin/python tools/test_proxy.py
```

Distinct clients must get distinct throttle budgets.

---

## 6. Cloudflare dashboard

- **SSL/TLS**: Full (strict); Always Use HTTPS; minimum TLS 1.2; HSTS **after**
  confirming HTTPS works end to end
- **Bots**: block AI scrapers and crawlers; Bot Fight Mode on
- **Rate limiting**: the free rule is best spent on `/login`
- **Access** (free to 50 users): protect `/ui/*` only. Two or three seats for you
  and the building manager adds a second factor on the surfaces that can create
  admin accounts, while tenants hitting `/t/…` and `/login` are untouched and
  consume no seats.
- Do **not** geo-block. Tempting for a building in Encino, but it locks you out
  from a hotel abroad, which is exactly when you need it.

---

## 7. Retire the TruPortal public IP

With `truportal.16400ventura.com` routing through the tunnel, remove the public
IP mapping at the firewall. That takes an access control appliance which has had
no vendor and no patches since 2020 off the open internet, while keeping the
remote access it is relied on for.

---

## Afterwards

- Rotate any password used during setup or testing
- Delete temporary accounts created for integration work
- Confirm a reboot brings back **both** the gateway and the tunnel
- `GET /health` should report every device online
