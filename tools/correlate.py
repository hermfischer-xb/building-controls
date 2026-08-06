#!/usr/bin/env python3
"""Do two suites share an air handler?

The question this was built for: Suite 321 held a +3F cooling offset for nine
hours on 2026-08-05, and the tenant in neighbouring 326 believes that is what
makes their office warm. The audit log cannot answer it -- it records the moments
somebody changed something, and the answer lives in the temperature between those
moments. The `reading` table is what makes it answerable.

Two independent readings of the same data, because either alone is easy to fool:

  lagged correlation   A's setpoint against B's temperature, at several delays.
                       Shared plant should show a peak at a positive lag of tens
                       of minutes -- B follows A, and not instantly.

  event study          what B's temperature actually did in the hours after each
                       time A was adjusted, against what it did on comparable
                       days when A was left alone.

Neither proves plumbing. Two suites on the same floor with the same sun exposure
and the same occupancy hours will correlate whatever the ductwork does, which is
why the event study is here: a shared unit should show B moving after A is
adjusted, not merely alongside it.

    .venv/bin/python tools/correlate.py 321 326
    .venv/bin/python tools/correlate.py 321 326 --days 7 --db data/bms.db
"""

from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bms.store import Store

# Lags to test, in minutes. A shared air handler responds over tens of minutes,
# so the interesting range is well past the sampling interval and well short of
# a working day.
LAGS = (0, 15, 30, 45, 60, 90, 120, 180)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation coefficient, or None when it is not defined.

    Returns None rather than 0.0 for a flat series. A suite whose setpoint never
    moved has no correlation to report, and reporting zero would read as
    "measured, and unrelated" instead of "nothing to measure".
    """
    if len(xs) < 3:
        return None
    try:
        sx, sy = statistics.stdev(xs), statistics.stdev(ys)
    except statistics.StatisticsError:
        return None
    if sx == 0 or sy == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)
    return cov / (sx * sy)


def series(store: Store, device_id: int, since: float) -> dict[float, dict]:
    """Samples keyed by timestamp, so two devices can be aligned by equality.

    The poller writes every device in a cycle under one shared timestamp, which
    is what makes this an exact lookup rather than a nearest-neighbour search.
    """
    return {r["ts"]: r for r in store.readings(device_id=device_id, since=since)}


def aligned(a: dict, b: dict, lag_minutes: float, field_a: str, field_b: str):
    """Pairs of (A at t, B at t + lag), for samples that exist on both sides."""
    lag = lag_minutes * 60
    xs, ys = [], []
    b_times = sorted(b)
    if not b_times:
        return xs, ys
    # Sampling is regular, so the nearest B sample to t+lag is within half an
    # interval. Tolerate that rather than requiring exact equality across a lag.
    step = (b_times[-1] - b_times[0]) / max(1, len(b_times) - 1)
    tol = max(step * 0.75, 60)
    for ts, row in sorted(a.items()):
        want = ts + lag
        lo = min(b_times, key=lambda t: abs(t - want))
        if abs(lo - want) > tol:
            continue
        x, y = row.get(field_a), b[lo].get(field_b)
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def adjustment_windows(a: dict, hours: float = 3.0) -> list[tuple[float, float]]:
    """When A moved into Temporary, and the window after each such moment."""
    events, was = [], None
    for ts in sorted(a):
        now = a[ts].get("setpoint_status")
        if was is not None and now == 3 and was != 3:
            events.append((ts, ts + hours * 3600))
        was = now
    return events


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("a", type=int, help="the suite whose thermostat gets adjusted")
    p.add_argument("b", type=int, help="the suite suspected of being affected")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--db", default="data/bms.db")
    args = p.parse_args()

    store = Store(args.db)
    since = time.time() - args.days * 86400
    a, b = series(store, args.a, since), series(store, args.b, since)

    span = store.reading_span()
    print(f"history holds {span.get('samples') or 0} samples "
          f"across {span.get('devices') or 0} device(s)\n")
    print(f"Suite {args.a}: {len(a)} samples    Suite {args.b}: {len(b)} samples")

    if len(a) < 30 or len(b) < 30:
        hours = (len(a) * 5) / 60
        print(f"\nNot enough history yet -- about {hours:.1f}h of samples for "
              f"Suite {args.a}.\nThis needs a couple of days of the sampler "
              f"running before it can say anything.\nCheck back then; nothing "
              f"below is worth reading with this little data.")
        store.close()
        return 0

    # --- lagged correlation -----------------------------------------------------
    print(f"\n--- Suite {args.a} effective setpoint vs Suite {args.b} space temp ---")
    print("  (a shared unit should peak at a positive lag of tens of minutes)\n")
    print(f"  {'lag':>6}  {'r':>7}  {'pairs':>6}")
    best = None
    for lag in LAGS:
        xs, ys = aligned(a, b, lag, "effective_sp", "space_temp")
        r = pearson(xs, ys)
        shown = "  n/a  " if r is None else f"{r:+.3f} "
        print(f"  {lag:>4}m  {shown}  {len(xs):>6}")
        if r is not None and (best is None or abs(r) > abs(best[1])):
            best = (lag, r)

    # A's own temperature against B's, as the control. If B tracks A's *setpoint*
    # no better than it tracks A's *temperature*, the two rooms are simply in the
    # same weather and nothing here is about ductwork.
    xs, ys = aligned(a, b, 0, "space_temp", "space_temp")
    ambient = pearson(xs, ys)

    print()
    if best is None:
        print("  Suite %d's setpoint never moved in this window -- nothing to correlate."
              % args.a)
    else:
        lag, r = best
        print(f"  strongest at {lag} minutes: r = {r:+.3f}")
        if ambient is not None:
            print(f"  the two rooms' temperatures alone: r = {ambient:+.3f}")
            if abs(r) <= abs(ambient) + 0.05:
                print("  -> no better than ambient. This looks like shared weather and")
                print("     shared occupancy hours, not shared plant.")
            else:
                print("  -> stronger than ambient, which is what shared plant looks like.")

    # --- event study ------------------------------------------------------------
    events = adjustment_windows(a)
    print(f"\n--- what Suite {args.b} did after each Suite {args.a} adjustment ---")
    if not events:
        print(f"  Suite {args.a} was never adjusted in this window.")
    else:
        print(f"  {'when':>17}  {'B start':>8}  {'B +3h':>8}  {'change':>7}")
        deltas = []
        for start, end in events:
            window = [r for ts, r in sorted(b.items())
                      if start <= ts <= end and r.get("space_temp") is not None]
            if len(window) < 2:
                continue
            first, last = window[0]["space_temp"], window[-1]["space_temp"]
            deltas.append(last - first)
            stamp = time.strftime("%m-%d %H:%M", time.localtime(start))
            print(f"  {stamp:>17}  {first:>8.1f}  {last:>8.1f}  {last - first:>+7.1f}")
        if deltas:
            print(f"\n  mean change in Suite {args.b} over 3h after an adjustment: "
                  f"{statistics.fmean(deltas):+.2f}F  (n={len(deltas)})")
            print("  Compare that against a typical 3h drift on a day with no")
            print("  adjustment before reading anything into it.")

    store.close()
    return 0


sys.exit(main())
