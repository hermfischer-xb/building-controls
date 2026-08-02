# Deploying to the on-premises controller

Written for a Mac mini sitting on the building network. Do these in order — the
last step depends on the one before it, and getting `secure_cookies` out of
sequence produces a login that silently fails.

---

## 1. Make macOS behave like an appliance

macOS defaults assume a desktop someone sits at. Four of them will take the
building offline if left alone.

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
git clone https://github.com/hermfischer-xb/building-controls.git
cd building-controls
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/devices.example.yaml config/devices.yaml
```

Three things are deliberately **not** in git and must be created here:
`config/devices.yaml`, `data/bms.db`, and `.venv`.

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

Create the first account and check it runs in the foreground before daemonising:

```bash
.venv/bin/python -m bms.useradmin add-admin herm     # blank prompt generates one
.venv/bin/python -m bms --config config/devices.yaml
```

Confirm it polls a thermostat, then Ctrl-C.

---

## 3. Run it as a LaunchDaemon

```bash
# edit the three CHANGEME paths first
sudo cp deploy/com.building-controls.gateway.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.building-controls.gateway.plist
sudo chmod 644       /Library/LaunchDaemons/com.building-controls.gateway.plist
sudo launchctl load -w /Library/LaunchDaemons/com.building-controls.gateway.plist

sudo launchctl list | grep building-controls
tail -f /usr/local/var/log/building-controls.log
```

A **Daemon**, not an Agent — agents wait for a login, so after an unattended
reboot the building would have no controller until someone sat down at it.

**Reboot the machine now and confirm it comes back on its own.** Better to find
out here than after the first power cut.

---

## 4. Cloudflare Tunnel

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create building-controls
cloudflared tunnel route dns building-controls controls.16400ventura.com
```

Put `deploy/cloudflared-config.yml` at `/etc/cloudflared/config.yml`, edit the
TruPortal LAN address, then:

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
