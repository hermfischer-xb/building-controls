"""The poll loop.

Sequential by default, and optionally concurrent. The single UDP socket is not a
reason to serialise: BACnet matches each response to its request on (invokeID,
source address) and bacpypes3 demultiplexes exactly that way, so requests to
different devices may be outstanding at once. What used to serialise everything
was a lock in BacnetClient, which this file's own comment then cited as the
reason concurrency would not help -- circular, since the lock was ours.

`poll_concurrency` now governs it, defaulting to 1 so the behaviour is unchanged
until someone opts in. Above 1 the devices are gathered and the client bounds how
many are actually in flight; it also serialises per device, so a reconciler write
never overlaps a poll read of the same thermostat.

The reason to raise it is that a slow device otherwise delays every device behind
it, and since retries were enabled a request can take six seconds. Sequentially
those add up; in parallel a cycle is roughly the slowest device rather than the
sum. The reason to be careful is that Wi-Fi is shared and half duplex, so a burst
of simultaneous requests is the wrong shape of traffic for a congested access
point -- watch avg_poll_ms after changing it.

**Budget a cycle from measured round-trips, not from the RPM figure.** This file
used to claim 19 ms per device, and therefore half a second for 25 devices, which
made a 10-second interval look generous. 19 ms was the read itself, measured on a
wired bench against one thermostat. Against 16 real units on the building's Wi-Fi
it is **~640 ms per device** — 33x more — so that same fleet takes ~10.2 s and
25 will take ~16 s. The difference is the wireless path and the thermostats'
own response latency, neither of which appears in a bench number.

At a 10-second interval the loop therefore never slept: it finished a cycle and
started the next immediately, logging an overrun every time, with no headroom for
the ~5 s a non-answering device costs. The building now runs
`poll_interval_seconds: 30`.

Two consequences worth holding on to:

- A cycle that overruns does not accumulate lag -- `_run` sleeps for whatever is
  left of the interval and no less than zero -- but it does mean the loop is
  saturated, and the warning it logs is the signal to raise the interval.
- `Cache(stale_after=poll_interval * 3)` scales with this, so a 30 s interval
  flags a dead device after 90 s rather than 30 s. That is an accepted trade: a
  suite's temperature does not move meaningfully in a minute, and any command
  re-reads immediately through `settle_and_refresh` rather than waiting for the
  next cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .bacnet import BACNET_FAULTS, BacnetClient, DeviceUnreachable
from .cache import Cache
from .config import Config
from .points import SetpointStatus
from .zones import Zones

log = logging.getLogger(__name__)

# Offsets are floats read off the wire, so compare with a tolerance. Equality
# would log a change every poll on a device reporting 2.0000001.
_OFFSET_EPSILON = 0.05
_OFFSET_KEYS = ("heat_adjust", "cool_adjust", "adjust")


class Poller:
    def __init__(self, cfg: Config, client: BacnetClient, cache: Cache,
                 zones: Zones, store=None) -> None:
        self._cfg = cfg
        self._client = client
        self._cache = cache
        self._zones = zones
        # Optional so the test harnesses can build a Poller without a database.
        self._store = store
        self._task: asyncio.Task | None = None
        self._cycle = 0
        self._overruns = 0
        # Sampled history. Both start at 0 so the first cycle after a restart
        # writes a sample and prunes -- a process that is restarted more often
        # than the retention window would otherwise never prune at all.
        self._last_sample = 0.0
        self._last_prune = 0.0

    async def start(self) -> None:
        for d in self._cfg.devices:
            self._cache.register(d.device_id, d.name, self._zones.of(d), d.address)

        # Stated plainly because a tuning that silently did not apply looks
        # exactly like a tuning that had no effect: set poll_concurrency to 3,
        # have the daemon read 1, and a quiet run reads as "no contention" from
        # something that never changed. Also on /health, for checking without
        # trawling the log.
        concurrency = self._client.concurrency
        log.info(
            "polling %d device(s) every %.0fs, %s, offline after %d consecutive failure(s)",
            len(self._cfg.devices),
            self._cfg.poll_interval_seconds,
            "one at a time" if concurrency == 1 else f"up to {concurrency} at a time",
            self._cfg.offline_after_failures,
        )
        if self._store is None or self._cfg.history_interval_seconds <= 0:
            log.info("history: not recording samples")
        else:
            log.info(
                "history: sampling every %.0fs, keeping %.0f days",
                self._cfg.history_interval_seconds,
                self._cfg.history_retention_days,
            )

        await self._verify_inventory()
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            # See Reconciler.stop: awaiting a task re-raises what killed it, and
            # a shutdown that aborts here skips every cleanup step after it.
            except (*BACNET_FAULTS, Exception):  # noqa: BLE001
                log.exception("poll task had already failed")
            self._task = None

    async def _verify_inventory(self) -> None:
        """Warn when a device's real BACnet id disagrees with the config.

        A fleet left on the factory default all reports 4194302, which is exactly
        the mistake worth catching at startup rather than at 2am.
        """
        for d in self._cfg.devices:
            actual = await self._client.read_device_id(d)
            if actual is None:
                log.warning("%s (%s) did not answer during inventory check", d.name, d.address)
            elif actual != d.device_id:
                log.warning(
                    "%s (%s) reports BACnet device id %d but config says %d%s",
                    d.name,
                    d.address,
                    actual,
                    d.device_id,
                    " -- still on the unconfigured factory default"
                    if actual >= 4194302
                    else "",
                )

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await self.poll_once()
            # Inside the timed section on purpose. Writing history is work the
            # cycle does, and hiding it would make a slow disk present as though
            # the radios had got worse.
            self._record_history()
            elapsed = time.perf_counter() - started
            self._cycle += 1

            if elapsed > self._cfg.poll_interval_seconds:
                self._overruns += 1
                # Same rule the offline-device path follows: log the condition,
                # not every cycle. A saturated loop is a standing state, and at a
                # 10s interval it produced six identical lines a minute forever,
                # which buries everything else in the log.
                if self._overruns == 1 or self._overruns % 20 == 0:
                    count = len(self._cfg.devices) or 1
                    # Suggest concurrency before a longer interval when still
                    # serial: a cycle usually overruns because one slow device
                    # delayed everything behind it, and polling less often makes
                    # the data staler without addressing that.
                    if self._client.concurrency == 1:
                        remedy = "set poll_concurrency to 3, or raise poll_interval_seconds"
                    else:
                        remedy = ("raise poll_interval_seconds to at least "
                                  f"{int((elapsed + 2 * self._cfg.request_timeout_seconds) / 10 + 1) * 10}")
                    log.warning(
                        "poll cycle took %.1fs, longer than the %.1fs interval "
                        "(%d devices, %.0f ms each) -- %s [%d consecutive]",
                        elapsed,
                        self._cfg.poll_interval_seconds,
                        count,
                        elapsed / count * 1000,
                        remedy,
                        self._overruns,
                    )
            elif self._overruns:
                log.info("poll cycle back within its interval after %d overrun(s)",
                         self._overruns)
                self._overruns = 0

            await asyncio.sleep(max(0.0, self._cfg.poll_interval_seconds - elapsed))

    async def poll_once(self) -> None:
        """One pass over the fleet.

        Sequential at `poll_concurrency: 1`, which is the default and the
        historical behaviour. Above that the devices are gathered and the client
        bounds how many are actually in flight, so concurrency is configured in
        exactly one place rather than negotiated between two.
        """
        if self._cfg.poll_concurrency <= 1:
            for device in self._cfg.devices:
                await self._poll_device(device)
            return
        await asyncio.gather(*(self._poll_device(d) for d in self._cfg.devices))

    def _note_setpoint_change(self, device, previous: dict, current: dict) -> None:
        """Record an occupant's temporary setpoint adjustment in the audit log.

        The cache is in memory and point-in-time: it shows that a suite is
        nudged right now, and loses the fact entirely when the occupant clears it
        or the schedule rolls over. Nothing else in the system would ever know it
        happened.

        Transitions only, not a sample every cycle. Thirty-five values every
        thirty seconds is a time-series problem and wants a table of its own;
        what a manager actually asks is "who has been fiddling with the heating",
        and that is answered by the handful of moments the answer changes.
        """
        if self._store is None:
            return

        def status(values: dict):
            try:
                return SetpointStatus(int(values["setpoint_status"]))
            except (KeyError, TypeError, ValueError):
                return None

        was, now = status(previous), status(current)
        if now is None:
            return

        offsets = {k: current.get(k) for k in _OFFSET_KEYS}
        moved = any(
            abs(float(current.get(k) or 0.0) - float(previous.get(k) or 0.0)) > _OFFSET_EPSILON
            for k in _OFFSET_KEYS
        )

        detail = {
            **{k: v for k, v in offsets.items() if v is not None},
            "effective_sp": current.get("effective_sp"),
            "space_temp": current.get("space_temp"),
        }

        if not previous:
            # First reading after a restart. Only worth a line if the suite is
            # already adjusted, so the fact survives the process that found it.
            if now is SetpointStatus.TEMPORARY:
                self._store.log("occupant", "setpoint.temporary.observed",
                                device.name, detail)
            return

        if was is None:
            # The previous poll succeeded but carried no status. That happens
            # because read_points_timed does not fail a whole poll when one
            # object errors -- it warns and omits the key -- so a poll can land
            # with everything except this point.
            #
            # Without this, the next poll compares None against the unchanged
            # status, calls that a transition, and writes a row saying an
            # occupant did something. A dropped datagram would forge an entry in
            # the record of who has been touching the thermostats, and it now
            # also decides what the history table is asked to explain. Wait for
            # two comparable readings instead; the real transition, if there is
            # one, is still there on the next cycle.
            return

        if was is not now:
            action = ("setpoint.temporary" if now is SetpointStatus.TEMPORARY
                      else "setpoint.scheduled")
            # `was` cannot be None here -- the guard above returned. Written
            # plainly so a null `from` in the database means something is wrong
            # rather than being a shape the code still expects to produce.
            self._store.log("occupant", action, device.name,
                            {**detail, "from": was.name, "to": now.name})
        elif moved and now is SetpointStatus.TEMPORARY:
            # Adjusted again without passing through a scheduled state.
            self._store.log("occupant", "setpoint.temporary.changed", device.name, detail)

    def _record_history(self) -> None:
        """Write one sample per device to the history table, on its own interval.

        Read from the cache once per cycle rather than from inside `_poll_device`.
        Three reasons, all of which bit something else in this file first:

        * `_poll_device` runs concurrently now. Writing there would interleave
          SQLite calls from several tasks on one connection.
        * Every device in a sample then shares a single timestamp, so "every
          suite at 14:05" is an equality rather than a range over a moving target.
        * A device that failed this cycle is simply absent from the sample. Its
          last good values are still in the cache and would otherwise be written
          again under a new timestamp, inventing a reading that never happened.
        """
        if self._store is None or self._cfg.history_interval_seconds <= 0:
            return
        now = time.time()
        if now - self._last_sample < self._cfg.history_interval_seconds:
            return
        self._last_sample = now

        rows = [
            (state.device_id, state.values)
            for state in self._cache.all()
            # `online` requires a successful poll on record, so this skips both a
            # device that has never answered and one that is currently dark.
            if state.online and state.values
        ]
        try:
            written = self._store.record_readings(now, rows)
        except Exception:  # noqa: BLE001 -- history must never stop the poll loop
            log.exception("could not write sampled history")
            return

        if self._cycle and written < len(self._cfg.devices):
            log.debug("history: sampled %d of %d device(s)",
                      written, len(self._cfg.devices))

        # Pruning is a daily job, not a per-cycle one: it is a full table scan
        # against an index, and running it every five minutes to delete nothing
        # is pure cost.
        if now - self._last_prune >= 86400:
            self._last_prune = now
            try:
                dropped = self._store.prune_readings(self._cfg.history_retention_days)
                if dropped:
                    log.info("history: pruned %d sample(s) older than %.0f days",
                             dropped, self._cfg.history_retention_days)
            except Exception:  # noqa: BLE001
                log.exception("could not prune sampled history")

    async def _poll_device(self, device) -> None:
        try:
            # Timed by the client, inside its gate, so a device queued behind
            # others is not recorded as a slow one. Per device rather than
            # per cycle because the fleet average hides the case of interest:
            # one unit on a weak signal among healthy neighbours. It is the
            # only link-quality signal available -- the thermostats do not
            # expose Wi-Fi RSSI over BACnet.
            values, elapsed_ms = await self._client.read_points_timed(device)
            # Copy the count, do not hold the object: `Cache.get` returns the
            # live DeviceState and `record_success` zeroes
            # `consecutive_failures` on it in place, so reading the attribute
            # afterwards always yields 0.
            state = self._cache.get(device.device_id)
            missed = state.consecutive_failures if state else 0
            # Snapshot before record_success overwrites it. The cache is the only
            # place the previous reading exists, and comparing the two is what
            # turns a point-in-time value into an event worth recording.
            previous = dict(state.values) if state else {}
            self._cache.record_success(device.device_id, values, elapsed_ms)
            self._note_setpoint_change(device, previous, values)
            if missed:
                # Recovery is worth a line at info: it distinguishes a unit
                # that flickers from one that is genuinely down, which is the
                # whole question when chasing a marginal radio link. With
                # `offline_after_failures` above 1 this is the *only* place a
                # flickering unit appears in the log at all, so the number
                # has to be right.
                log.info("%s answered again after %d missed poll(s)",
                         device.name, missed)
        except DeviceUnreachable as err:
            self._cache.record_failure(device.device_id, str(err))
            state = self._cache.get(device.device_id)
            # Log the transition into offline, not every cycle, or an offline
            # thermostat produces a line every interval forever. Recorded
            # first so the threshold is tested against the new count: firing
            # on the *first* miss is what filled the log with units that had
            # simply lost a datagram and were fine on the next pass.
            if state and state.consecutive_failures == self._cfg.offline_after_failures:
                log.warning(
                    "%s went offline after %d consecutive failures: %s",
                    device.name, state.consecutive_failures, err,
                )
        # BACNET_FAULTS as well as Exception: bacpypes3's errors derive from
        # BaseException, so `except Exception` alone does not stop one bad device
        # killing the loop, which is the entire job of this handler.
        except (*BACNET_FAULTS, Exception):  # noqa: BLE001
            log.exception("unexpected error polling %s", device.name)
            self._cache.record_failure(device.device_id, "internal error")
