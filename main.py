"""Hour-by-hour forecast of GTA VI's launch-day Twitch viewers (make.fun bounty).

The model anchors on hour 1's print and borrows the day's shape from past big
launches. Every comparable launch in the case file contributes its release
day's hourly viewer counts, turned into log-ratios against that launch's own
hour 1. For each forecast hour the model takes a weighted median of those
log-ratios as the centre, and weighted empirical quantiles of the same
distribution as the spread, shrunk a little toward the centre: the raw
cross-launch spread is wider than the pinball loss rewards (tuned by
leave-one-out validation over the comparable launches themselves).

Comparables are filtered and weighted by how much their launch resembles a
big console launch that unlocked at midnight local time:
  * a launch whose hour-1 print is under 5% of its day peak is dropped: its
    hour 1 was effectively pre-unlock, a different regime;
  * launches flagged early-access-contaminated or weak in their unlock notes
    count at 0.3 weight;
  * the target's own pre-launch category history is not a launch at all and
    drops out on its own (no hour-1 print in its window).

Everything is deterministic: no randomness, no wall-clock or hash-order
dependence, input files processed in name order. If the comparables are
unusable, or anything goes wrong for a case, the forecast falls back to a flat
persistence forecast with a widening spread, so every case still gets a valid
output file. Standard library only, no network.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The quantile levels the contest scores, in the order each row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# The standard normal's quantile at each level (used by the fallback only).
Z_SCORES = [-1.645, -0.674, 0.0, 0.674, 1.645]
# How much of each quantile's log-distance from the centre survives (tuned).
SPREAD_SHRINK = 0.85
# A comparable whose hour 1 is under this share of its day peak is dropped.
MIN_H1_PEAK = 0.05
# Weight for early-access-contaminated or weak-tier comparables.
TIER_DOWNWEIGHT = 0.3
# The fallback spread, in log units per square-root hour.
FALLBACK_SIGMA = 0.15
# A quantile may not exceed this many viewers.
MAX_VIEWERS = 1_000_000_000


def parse_instant(text):
    """An ISO 8601 timestamp as an aware datetime, or None if unparseable."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def release_day_curve(comp):
    """A comparable's release-day hourly viewers, hours 1..24, or None.

    The day runs from the comparable's day_start for 24 whole hours. A curve
    counts only when every one of those hours has a valid point; anything
    less (a sparse pre-launch history, a missing hour) is unusable.
    """
    try:
        day_start = parse_instant(comp.get("day_start"))
        if day_start is None:
            return None
        curve = {}
        for point in comp.get("hourly") or []:
            if not isinstance(point, dict):
                continue
            at = parse_instant(point.get("from"))
            viewers = point.get("viewers")
            if at is None or not isinstance(viewers, (int, float)):
                continue
            if isinstance(viewers, bool) or not math.isfinite(viewers):
                continue
            if viewers < 0:
                continue
            delta = at - day_start
            seconds = delta.days * 86400 + delta.seconds
            if 0 <= seconds < 86400 and seconds % 3600 == 0:
                curve[seconds // 3600 + 1] = max(1.0, float(viewers))
        if len(curve) != 24:
            return None
        return curve
    except Exception:
        return None


def comparable_weight(comp, curve):
    """How much a comparable's curve counts: 0 drops it, 1 is full weight."""
    peak = max(curve.values())
    if curve[1] / peak < MIN_H1_PEAK:
        return 0.0
    notes = str(comp.get("unlock") or "").lower()
    if "target-history" in notes:
        return 0.0
    if "early-access-contaminated" in notes or "early-access contaminated" in notes:
        return TIER_DOWNWEIGHT
    if "tier: weak" in notes:
        return TIER_DOWNWEIGHT
    return 1.0


def weighted_quantile(pairs, tau):
    """Smallest value whose cumulative weight reaches tau of the total weight.

    `pairs` is a list of (value, weight). Returns None when the total weight
    is zero or there are no pairs.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return None
    cutoff = tau * total
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= cutoff:
            return value
    return ordered[-1][0]


def clamp_and_round(value):
    """A quantile as a viewer count: finite, in [1, 1e9], 2 decimals, half up.

    Whole numbers come back as ints, so the JSON writes 1240, not 1240.0.
    """
    if not math.isfinite(value):
        value = 1.0
    value = min(max(value, 1.0), float(MAX_VIEWERS))
    rounded = math.floor(value * 100.0 + 0.5) / 100.0
    rounded = min(max(rounded, 1.0), float(MAX_VIEWERS))
    return int(rounded) if rounded == int(rounded) else rounded


def ratio_forecast(p1, pool, hours):
    """The comparable-ratio forecast: {hour: [q05, q25, q50, q75, q95]}.

    `pool` is a list of (curve, weight). Returns None when any hour has no
    usable ratios, so the caller can fall back.
    """
    out = {}
    for hour in hours:
        pairs = []
        for curve, weight in pool:
            if hour in curve and 1 in curve:
                pairs.append((math.log(curve[hour] / curve[1]), weight))
        if not pairs:
            return None
        centre = weighted_quantile(pairs, 0.5)
        # Safety rail: however small the pool, never claim more certainty
        # than a quarter of a random walk's spread for the horizon. With a
        # full pool the empirical spread is wider than this everywhere, so
        # it never binds on a real case (verified: byte-identical outputs);
        # it only stops a tiny pool (one or two comparables) collapsing all
        # five quantiles onto one value.
        rail = 0.25 * FALLBACK_SIGMA * math.sqrt(max(hour - 1, 0))
        row = []
        for tau, z in zip(LEVELS, Z_SCORES):
            raw = weighted_quantile(pairs, tau)
            shrunk = centre + (raw - centre) * SPREAD_SHRINK
            if z < 0:
                shrunk = min(shrunk, centre - abs(z) * rail)
            elif z > 0:
                shrunk = max(shrunk, centre + abs(z) * rail)
            row.append(clamp_and_round(p1 * math.exp(shrunk)))
        for i in range(1, len(row)):
            if row[i] < row[i - 1]:
                row[i] = row[i - 1]
        out[hour] = row
    return out


def persistence_forecast(p1, hours):
    """The flat fallback: every hour stays at hour 1's print, spread widening
    like a random walk in log space. Always valid, whatever the inputs."""
    out = {}
    for hour in hours:
        sigma = FALLBACK_SIGMA * math.sqrt(max(hour - 1, 0))
        row = [clamp_and_round(p1 * math.exp(z * sigma)) for z in Z_SCORES]
        for i in range(1, len(row)):
            if row[i] < row[i - 1]:
                row[i] = row[i - 1]
        out[hour] = row
    return out


def hour_one_print(case):
    """Hour 1's print from the case's `known` rows (1 when absent or invalid)."""
    for row in case.get("known") or []:
        if not isinstance(row, dict) or row.get("hour") != 1:
            continue
        viewers = row.get("viewers")
        if isinstance(viewers, (int, float)) and not isinstance(viewers, bool):
            if math.isfinite(viewers):
                return max(1.0, float(viewers))
    return 1.0


def forecast_hours(case):
    """The hours to forecast: the case's list when it is exactly 2..24,
    otherwise 2..24. The output must hold exactly those 23 rows."""
    wanted = set(range(2, 25))
    found = set()
    for row in case.get("forecast") or []:
        if isinstance(row, dict) and isinstance(row.get("hour"), int):
            found.add(row["hour"])
    if found == wanted:
        return sorted(found)
    return sorted(wanted)


def build_pool(case):
    """The usable comparables: (release-day curve, weight) pairs."""
    pool = []
    for comp in case.get("comparables") or []:
        if not isinstance(comp, dict):
            continue
        curve = release_day_curve(comp)
        if curve is None:
            continue
        weight = comparable_weight(comp, curve)
        if weight > 0:
            pool.append((curve, weight))
    return pool


def forecast_case(case):
    """One case's forecast object, always of the required shape."""
    p1 = hour_one_print(case)
    hours = forecast_hours(case)
    result = None
    pool = build_pool(case)
    if pool:
        try:
            result = ratio_forecast(p1, pool, hours)
        except Exception:
            result = None
    if result is None:
        result = persistence_forecast(p1, hours)
    return {"hours": [{"hour": hour, "q": result[hour]} for hour in hours]}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_DIR.is_dir():
        return
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(case, dict):
                case = {}
        except Exception:
            case = {}
        try:
            forecast = forecast_case(case)
        except Exception:
            # Last resort: a flat forecast around 1 viewer still beats no file.
            rows = persistence_forecast(1.0, list(range(2, 25)))
            forecast = {"hours": [{"hour": h, "q": rows[h]} for h in range(2, 25)]}
        out_path = OUTPUT_DIR / (path.stem + ".out")
        out_path.write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
