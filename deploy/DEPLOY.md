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
.venv/bin/python -m pip install --upgrade pip     # before anything else
.venv/bin/python -m pip install -r requirements.txt
cp config/devices.example.yaml config/devices.yaml
```

Three things are deliberately **not** in git and must be created here:
`config/devices.yaml`, `data/bms.db`, and `.venv`.

**Upgrade pip first**, then check the architecture if the install still wants to
compile:

```bash
uname -m          # arm64 -> wheels exist;  x86_64 -> read on
```

On an **Intel Mac there is no wheel for `cryptography`** and there will not be
one. Every macOS wheel it has published since version 49 is
`macosx_11_0_arm64`; the last `universal2` build was 48.0.1. pip therefore falls
back to source and asks for a Rust toolchain, and no amount of upgrading pip
changes that.

Install Rust and let it build:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
.venv/bin/python -m pip install -r requirements.txt
```

Keep it afterwards -- any future upgrade of `cryptography` rebuilds. `rustup`
lives in `~/.cargo` and does not touch the system.

The tempting alternative is pinning `cryptography==48.0.1` for its universal2
wheel, but that means running an old version of the library that verifies
passkey signatures for door unlock. A one-time build is the better trade.

This is not specific to one package: Intel Macs are steadily losing wheel
coverage, so expect it again elsewhere.

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
not. Two files need restricting, and both must be owned by whatever account the
daemon runs as -- mode 600 owned by the wrong user just stops it starting:

```bash
sudo chown -R <daemon-user> data config/devices.yaml
sudo chmod 700 data && sudo chmod 600 data/bms.db config/devices.yaml
```

`bms.db` holds password hashes and every live session token. `devices.yaml`
holds the access panel's credentials **in clear text**, and they cannot be
hashed: the daemon presents them on every call, so it must be able to recover
them. Any scheme that lets it do so -- Keychain, an encrypted file, an
environment variable -- also lets anyone with equivalent access. Environment
variables are worse, since a LaunchDaemon's environment lives in a
world-readable plist. With FileVault off the disk is not encrypted at rest
either, so file permissions and physical security of the machine are the actual
controls. The gateway warns at startup if either file is too permissive.

**Consider a dedicated service account.** If the daemon runs as your own login
and you also use the machine interactively, anything else running as you can
read both files. A non-login account closes that:

```bash
sudo sysadminctl -addUser _bms -fullName "Building Controls" \
     -home /var/empty -shell /usr/bin/false
sudo dscl . -create /Users/_bms IsHidden 1
sudo chown -R _bms /Users/Shared/building-controls
sudo chown _bms /usr/local/var/log/building-controls.log   # NOT under the repo
```

That last line is the one everybody forgets. `chown -R` over the repo cannot
reach the log, because `StandardOutPath` points outside it -- and launchd opens
that file **as the daemon's user, before exec**. Get it wrong and the service
never starts, with no traceback anywhere, because the only place the error could
be written is the file it cannot open. Whenever `UserName` changes, three things
follow it: `data/`, `config/devices.yaml`, and the log.

Set `UserName` to `_bms` in the plist and re-bootstrap. Note it will need the
Local Network Privacy grant again -- that is per identity, and granting it for
an account with no GUI session is the fiddly part, so do it when you can
iterate rather than immediately before you need the system working.

Reverting is the same trap in reverse. `sudo chown -R hermf:staff .` inside the
repo looks complete and leaves the log still owned by the account you abandoned.

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

# Startup is not instant: the inventory check contacts every thermostat, and any
# that does not answer costs the full BACnet retry budget before it gives up.
# Measured on the mini with one dark unit out of sixteen: ~17 s. Poll until it
# answers rather than sleeping a fixed number of seconds, which only has to be
# retuned the next time a thermostat goes dark.
until curl -fsS localhost:8237/health; do sleep 2; done
```

Use `bootstrap`, not `load -w`. `load` is deprecated on macOS 15 and reports
failure as `Load failed: 5: Input/output error`, which says nothing about the
cause. To stop or replace it: `sudo launchctl bootout
system/com.building-controls.gateway`.

Use `curl -sS`, not `-s`. Plain `-s` swallows the error too, so a failed check
looks identical to a check that printed nothing.

A **Daemon**, not an Agent — agents wait for a login, so after an unattended
reboot the building would have no controller until someone sat down at it.

### If it will not start and there is no traceback

```
Bootstrap failed: 5: Input/output error
```

with `sudo launchctl print system/com.building-controls.gateway` showing `state
= spawn scheduled` and `active count = 0`, and **nothing new in the log**, means
launchd could not open `StandardOutPath`/`StandardErrorPath` as the account in
`UserName`. Python never ran. Check it directly:

```bash
ls -l /usr/local/var/log/building-controls.log   # owner must match UserName
```

The absence of a traceback is the diagnostic signal, not a dead end: the error's
only destination is the file it cannot open. `kickstart -k` and `bootout` +
`bootstrap` both fail the same way, because neither touches the cause.

Note that **running it in the foreground will succeed and prove nothing** -- a
foreground run writes to your terminal and never opens `StandardOutPath` at all.
This failure exists only under launchd.

### Install the launchd jobs

Both plists ship with `CHANGEME` placeholders. **Do not hand-edit them.** A
missed one is not a typo you notice: launchd rejects a job whose `UserName` does
not exist with `EX_CONFIG` *before* it opens the log file, so the only symptom is
a job that never runs and never explains itself.

Run this from the repo root, as the account the daemons should run as:

```bash
REPO="$PWD"
for job in gateway backup; do
  sed -e "s|/Users/CHANGEME/building-controls|$REPO|g" \
      -e "s|<string>CHANGEME</string>|<string>$(whoami)</string>|g" \
      "deploy/com.building-controls.$job.plist" > "/tmp/$job.plist"
  plutil -lint "/tmp/$job.plist" || break
  sudo install -o root -g wheel -m 644 "/tmp/$job.plist" \
      "/Library/LaunchDaemons/com.building-controls.$job.plist"
done
```

Two substitutions, not one: the repo path *contains* `CHANGEME`, so a single
`s/CHANGEME/$(whoami)/` would produce `/Users/hermf/building-controls` on a
machine whose repo is at `/Users/Shared/building-controls`. Order matters — path
first, then the bare `UserName`.

Create the log and backup directories, owned by that same account, **before**
bootstrapping. launchd opens `StandardOutPath` as `UserName`, and `/usr/local/var`
is root-owned on any machine that never installed Homebrew:

```bash
sudo mkdir -p /usr/local/var/log /usr/local/var/backups/building-controls
sudo chown "$(whoami)" /usr/local/var/log /usr/local/var/backups/building-controls

sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.gateway.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.backup.plist
sudo launchctl kickstart -k system/com.building-controls.backup   # don't wait for 03:15
```

Then confirm what launchd actually resolved, rather than what you meant:

```bash
sudo launchctl print system/com.building-controls.backup | grep -E "username|program|exit"
```

### If a job never runs and writes no log

`backup.sh` prints on every path, so **an empty or missing
`/usr/local/var/log/building-controls-backup.log` does not mean "it ran
quietly"** — it means no process ever started. `launchctl print` names which:

| In `launchctl print` | Cause |
|---|---|
| `last exit code = 78: EX_CONFIG`, `username = CHANGEME` | the account does not exist. Rejected at user lookup, before the log was opened — hence silence rather than an error |
| `state = spawn scheduled`, `active count = 0`, no exit code | launchd could not open `StandardOutPath` as `UserName`. Check `ls -ld /usr/local/var/log` |
| `Could not find service` | never bootstrapped. Copying a plist does not install it, and `kickstart` on a service that does not exist creates nothing |

`properties = penalty box` means launchd has throttled it after repeated
failures. `bootout` then `bootstrap` clears it; **`kickstart` alone does not**, so
a fixed plist can still look broken until it is properly reloaded:

```bash
sudo launchctl bootout system/com.building-controls.backup
sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.backup.plist
```

To prove the script itself works, independently of launchd:

```bash
deploy/backup.sh ~/backup-test && ls -R ~/backup-test
```

That distinguishes a broken script from a broken job, and it is the faster test.
Unlike the gateway, this one *is* meaningful in the foreground — the script's only
launchd-specific dependency is where its output goes.

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

`tunnel login` opens a browser and then polls for about eight minutes before
giving up with `Failed to write the certificate` — harmless, nothing is written,
just run it again. It also requires a **verified account email**; if the
authorize page refuses, verify at Cloudflare's My Profile → Email Address →
*Send verification email*, then restart the login, since the callback token will
have expired in the meantime.

`tunnel create` writes `~/.cloudflared/<UUID>.json`. Note the UUID — the config
below wants it.

Put `deploy/cloudflared-config.yml` at `/etc/cloudflared/config.yml` and edit the
TruPortal LAN address — **or, if TruPortal is deferred, comment that whole
ingress block out.** cloudflared validates ingress at startup and will not come
up with `CHANGEME-TRUPORTAL-LAN-IP` in the file. Skip its `tunnel route dns`
line too, so the hostname does not exist until there is something behind it;
steps 4 and 7 then cover `controls.16400ventura.com` only.

Install the credentials root-only — they authenticate the tunnel, so treat them
like a private key:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/building-controls.json
sudo chown root:wheel /etc/cloudflared/building-controls.json
sudo chmod 600       /etc/cloudflared/building-controls.json
sudo cp deploy/cloudflared-config.yml /etc/cloudflared/config.yml

cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
sudo cloudflared service install
```

**`service install` writes a plist that does not run the tunnel.** It builds
`ProgramArguments` from `~/.cloudflared/config.yml`; with the config in `/etc`
instead, it emits the bare binary path with no subcommand, which prints usage and
exits. The daemon then respawns forever, `tunnel info` reports no connections,
and the only clue is `use 'cloudflared tunnel run' to start tunnel …` in
`/Library/Logs/com.cloudflare.cloudflared.err.log`.

Fix `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` so
`ProgramArguments` reads:

```xml
<string>/usr/local/bin/cloudflared</string>
<string>--config</string>
<string>/etc/cloudflared/config.yml</string>
<string>tunnel</string>
<string>run</string>
```

Set `KeepAlive` to `true` while you are in there. The installer's default only
restarts on a *failing* exit, which would leave the building with no remote
access after any exit cloudflared considers clean. Then:

```bash
sudo launchctl bootout system/com.cloudflare.cloudflared
sudo launchctl bootstrap system /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
cloudflared tunnel info building-controls        # want a connector and its edge locations
```

Outbound only — no inbound port, no firewall change, and the public IP stops
being an attack surface.

Test <https://controls.16400ventura.com> before continuing. `/` should redirect
to `/login`, and `/health` should answer with your device count.

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
- **Rate limiting**: spend the one free rule on `POST /login` — see below
- **Access** (free to 50 users): protect `/ui/*` only. Two or three seats for you
  and the building manager adds a second factor on the surfaces that can create
  admin accounts, while tenants hitting `/t/…` and `/login` are untouched and
  consume no seats.
- Do **not** geo-block. Tempting for a building in Encino, but it locks you out
  from a hotel abroad, which is exactly when you need it.

### The one free rate-limiting rule

Put it on the login endpoint, counting by IP:

```
Expression:  http.request.uri.path eq "/login"
Counting:    by IP address (the free plan's only characteristic)
Threshold:   10 requests
Period:      the shortest offered (free plans typically expose only 10 seconds)
Action:      Block, for the longest mitigation timeout offered
```

**The free plan does not offer `http.request.method`** in a rate-limiting
expression — the builder shows only URI path, Operation ID and bot fields, and
method is Pro and above. So the rule cannot be scoped to the form *submission*
and will also count the page load. Two consequences:

- The threshold has to absorb it. One sign-in is two requests to this path (`GET`
  the form, `POST` it); a fumbled password adds one each. **10 per 10 seconds**
  leaves room for several people arriving together on the building Wi-Fi, who all
  share one public address and are counted as one client.
- Scanners doing `GET /login` now consume the budget too, which is mostly fine —
  they are the traffic this exists for — but it is why the threshold cannot be 5.

If you ever want a tighter rule, the fix is on this side rather than
Cloudflare's: give the form submission its own path, so path-only matching
becomes exact and the threshold can drop to 5. Not done, because it splits the
authentication route to work around a plan limitation, and 10 already changes a
sprayer's economics.

**Why this endpoint and not the doors.** `POST /doors/{id}/unlock` looks like the
thing worth protecting, but it already requires a session cookie *and* a passkey
assertion inside 90 seconds; an anonymous attacker gets a 401 and never reaches
the panel. Rate limiting it would mostly risk blocking someone standing at a door
in the rain. `/login` is the only endpoint where an unauthenticated stranger can
do unlimited useful work.

**Why it is not redundant with `LoginThrottle`.** The in-process throttle keys on
`username|client` ([bms/auth.py](../bms/auth.py)), which means it catches someone
hammering *one* account — and never fires for someone spraying one password
across many usernames from a single address, because no individual key reaches 8.
Cloudflare counting by IP alone closes precisely that gap. The two are
complementary, and neither substitutes for the other: the app's throttle survives
attacks that never reach Cloudflare, and Cloudflare's survives a gateway restart,
which resets the in-process one to zero.

**What 10 per 10 seconds is actually worth.** It does not stop a patient attacker
— nothing on one free rule does. It caps a sprayer at ~60 requests a minute
instead of thousands, and that is the difference between working through the
building's ~20 usernames a few passwords a minute and doing it at machine speed.
Anyone already signed in is unaffected either way, because sessions last 30 days
and never touch this path. Tenants on cellular have their own addresses.

**Verify it, in a browser you can afford to lock out for a minute**: reload the
login page and submit a wrong password until you have made a dozen requests
inside ten seconds. You should get Cloudflare's block page rather than the app's
"Incorrect username or password." If you only ever see the app's message, the
rule is not matching — check it is scoped to the hostname, and that `/login` is
the path Cloudflare sees rather than something rewritten by the tunnel.

---

## 7. Retire the TruPortal public IP

The appliance has had no vendor and no patches since 2020, and it opens doors.
Anyone on the internet can currently reach its login page, and Shodan indexes
hosts like it continuously. This is the largest single risk in the building, and
it is larger than anything in this application.

### The fix is one static IP change on the panel itself

**Give it a private static address on the office LAN — `192.168.1.65` or
similar — and change nothing else.** No tunnel, no Cloudflare Access, no
firewall rules, no router reprogramming.

```yaml
truportal:
  host: 192.168.1.65
```

That is the whole thing. The public address is configured *on the panel*, so
taking it off the internet is done on the panel, not in the AT&T router — which
matters here because that router is not readily reprogrammable.

**Why the office LAN and not the thermostat network.** The panel needs a default
gateway for anything outbound it does (NTP, SMTP alerts), and the isolated
control segment deliberately has no route off it. `192.168.1.0/24` has the
router as its gateway, so the panel gets outbound NAT and no inbound path, which
is exactly the shape wanted. Put it outside the DHCP pool, or reserve it, so
nothing else takes the address.

The gateway reaches it from its office-LAN interface. Nothing about the BACnet
side changes.

### Remote administration: use the mini, not a tunnel

There is no reason to publish this panel at all. **Remote into the mini and use
its browser** — it is already on the inside network, and remote access to it is
already solved and already protected.

This is strictly better than the alternative, not merely simpler: nothing is
published, no hostname enters a Certificate Transparency log, and there is no
Access policy that can be misconfigured into an open door. It also needs no
Cloudflare seats.

**Do not put this panel behind a tunnel hostname.** If you ever reconsider,
understand what a tunnel does and does not do: publishing
`truportal.16400ventura.com` with no policy in front moves the front door from
an IP address to a hostname, and CT logs publish that hostname within hours — so
it trades discovery-by-IP-scan for discovery-by-CT-log, which is faster. Only
Cloudflare Access in front of it would make that safe, by terminating
unauthenticated requests at the edge. Since remoting into the mini already
solves the problem, that is complexity bought for nothing.

### Verify it, from outside

Changing the address is not evidence the exposure is gone. From a phone on
cellular, with Wi-Fi off:

```bash
curl -sS --max-time 10 -k https://<the old public IP>/     # want a timeout
```

A timeout or refused connection is the result. A login page means something —
an IP passthrough setting, a DMZ host, or a port forward — is still pointing at
it. Check the router for a forward aimed at the panel's *new* address too, since
a rule written against `192.168.1.65` would re-expose it just as effectively.

### What this does and does not fix

Fixed: the entire internet can no longer reach an unpatched access control
appliance. That is the whole threat model for a device with a public IP.

Not fixed, and worth knowing:

- **Anyone already on the office LAN can still reach it.** That is the trade for
  keeping it somewhere with a default gateway, and it is a good trade against a
  public IP. If tenants are ever put on this same network rather than their own
  SSID, revisit it — a VLAN or a segment of its own would then be worth the
  complexity.
- **`truportal.password` is still cleartext** in `config/devices.yaml` (mode 600).
  A LAN address reduces who can use it; it does not stop someone who reads it.
- **The mini becomes the only remote path to the panel.** Acceptable — on-site
  access is unaffected by a mini failure — but know it before you rely on it.
- **Check IP forwarding is off** on the mini. It is dual-homed, and a host that
  forwards packets between the office LAN and the control network undoes the
  isolation the VLAN is for:

```bash
sysctl net.inet.ip.forwarding          # want 0
sudo sysctl -w net.inet.ip.forwarding=0
```

  Nothing here routes: the gateway is an application-layer bridge that terminates
  HTTP on one side and speaks BACnet and SOAP on the other, so it never needs to
  forward a packet.

---

## Afterwards

- Rotate any password used during setup or testing
- Delete temporary accounts created for integration work
- Confirm a reboot brings back **both** the gateway and the tunnel
- `GET /health` should report every device online
