# Layover Planner — v0 Spec

## 1. What this is

A search tool for budget travellers who are flexible on time. It finds round-trip
fares where one direction contains a layover long enough to leave the airport,
scores how usable that dead time actually is, and generates a plan for it.

**The wedge:** we serve involuntary long layovers on cheap tickets. Airlines sell
*voluntary* stopovers as an upsell on premium hubs. Nobody serves the traveller
who already bought the ugly 24-hour return because it was half the price.

**One-line pitch:** *You already bought this ticket. Here's how to make the dead
time worth something.*

Hidden-city search is a v2 feature. It is out of scope for v0 and must not be
built yet — see §11.

---

## 2. v0 scope — deliberately narrow

| Axis | v0 scope | Why |
|---|---|---|
| Passport | Nordic (NO/SE/DK/FI/IS) | All five have identical entry rights at all six hubs. Collapses the visa matrix to ~6 hand-verifiable cells. |
| Origin | OSL only | Fits the Amadeus free-tier call budget. |
| Hubs | IST, DOH, DXB, AUH, WAW, RIX | Two Schengen, four non-Schengen — exercises both immigration paths. |
| Destinations | 10 (see `hubs.seed.json`) | Call budget. |
| Date window | 30 days forward, refreshed weekly | Call budget. |
| Accommodation | Read-only recommendations, no booking | Booking integration is v1. |

Everything outside this table is a v1+ decision. Do not widen scope to "make it
general" — the narrow version is what proves the concept.

---

## 3. Stack decision

**Python 3.12 + FastAPI + SQLite + Jinja2 templates.**

Rationale: the hard parts are a nightly batch job and a scoring function, both of
which are Python's strength. A server-rendered frontend is enough for v0 and
removes an entire build pipeline. SQLite is sufficient at this data volume and
makes the whole thing a single file you can copy.

**Assumption to challenge before writing code:** if you intend to hire a frontend
person or want a mobile app within six months, TypeScript end-to-end (Next.js +
Prisma) is the better call and this decision should be revisited now, not later.

No Docker for v0. No auth. No user accounts.

---

## 4. Data model

```python
# --- Reference data (seeded, hand-verified) ---

@dataclass
class Hub:
    iata: str                      # "IST"
    city: str
    country_iso2: str
    is_schengen: bool
    transfer_minutes: int          # airport -> city centre, one way, public transit
    transfer_cost_eur: float       # one way
    transfer_mode: str             # "metro" | "train" | "bus"
    transfer_note: str | None      # operational caveats, free text
    disembark_minutes: int         # wheels-down to landside door, excl. immigration
    immigration_minutes: int       # only applied when this is an entry point
    recheck_buffer_minutes: int    # required presence before onward departure
    has_left_luggage: bool
    activity_density: float        # 0.0-1.0, hand-scored

@dataclass
class EntryRule:
    passport_scope: str            # "NORDIC" — all five passports of §2, one row
    country_iso2: str              # "TR"
    entry_type: str                # "schengen_internal" | "visa_free" | "voa" |
                                   # "evisa" | "visa_required" | "no_landside_access"
    max_stay_days: int | None
    passport_validity_days: int    # required beyond arrival date
    notes: str
    verified_on: date | None       # null until checked — gates rendering, see §7

# --- Fare data (fetched) ---

@dataclass
class Segment:
    carrier: str
    flight_number: str
    origin: str
    destination: str
    departure_local: datetime      # tz-aware
    arrival_local: datetime        # tz-aware
    is_international: bool

@dataclass
class Itinerary:
    id: str
    price_eur: float
    is_single_ticket: bool         # False => self-transfer, bags not through-checked
    outbound: list[Segment]
    inbound: list[Segment]

# --- Computed ---

@dataclass
class Layover:
    hub_iata: str
    arrival: datetime              # tz-aware, local to hub
    departure: datetime
    gross_minutes: int
    usable_minutes: int            # after all deductions, §5
    daylight_minutes: int          # overlap of usable window with local 08:00-21:00
    is_entry_point: bool           # do we clear immigration here?
    requires_bag_reclaim: bool
    band: str                      # NO_EXIT | QUICK | HALF_DAY | FULL_DAY | OVERNIGHT
    score: int                     # 0-100
    blocked_reason: str | None

@dataclass
class Comparison:
    itinerary_price_eur: float
    baseline_price_eur: float      # cheapest fast itinerary, same OD + dates
    fare_saving_eur: float
    layover_plan_cost_eur: float   # transfers + activities + stay
    net_saving_eur: float          # fare_saving - plan_cost
    extra_hours: float
```

`net_saving_eur` is the number the whole product exists to display. If it's
negative the itinerary is honestly worse and we say so.

---

## 5. Scoring — the core logic

### 5.1 Usable time

```python
def recheck_buffer(hub: Hub, onward: Segment) -> int:
    # A Schengen hub's 120 assumes an intra-Schengen departure. Leaving the zone
    # means full exit control — which is what the non-Schengen 180 already covers.
    if hub.is_schengen and not is_schengen_airport(onward.destination):
        return 180
    return hub.recheck_buffer_minutes


def usable_minutes(lay: Layover, hub: Hub, onward: Segment) -> int:
    m = lay.gross_minutes
    m -= hub.disembark_minutes
    if lay.is_entry_point:
        m -= hub.immigration_minutes
    if lay.requires_bag_reclaim:
        m -= 30
    m -= hub.transfer_minutes * 2
    m -= recheck_buffer(hub, onward)
    m -= SAFETY_MARGIN            # 45
    return max(0, m)
```

There is no `exit_control_minutes` deduction. Departure passport control is real
and it does cost ~25 minutes at DXB — but it is already inside
`recheck_buffer_minutes`. The 180 is the airline's own "be at the airport three
hours before an international departure", which covers check-in, bag drop,
security, exit control and the walk to the gate. Deducting it again counts the
same queue twice. The 180-vs-120 split between non-Schengen and Schengen hubs
encodes exactly that difference, which is why `recheck_buffer()` returns 180 for
a Schengen hub whose onward leg leaves the zone: `OSL → WAW → BKK` faces full
Schengen exit control at Warsaw and must not be costed at 120. It mirrors the
entry-point rule in §5.2 — the direction of travel decides, not the hub.

`SAFETY_MARGIN = 45`. Be conservative. Being wrong here means someone misses a
flight.

The margin was 30, with a further 60 subtracted when the onward flight was the
last departure of the day. That flag is not computable: knowing whether a
departure is the day's last to a destination needs full schedule data for the
hub, and a `flight-offers` response only describes the itineraries it returned.
It is removed, and the margin absorbs the risk it covered — strictly simpler and
strictly more conservative. Reinstating it is a v1 item, conditional on adding a
schedule source. See §11.

### 5.2 Entry point determination

This is the rule most tools get wrong. Immigration happens at the **first point
of entry into the customs union**, not at the final destination.

```python
def is_entry_point(hub: Hub, arriving_from: Segment) -> bool:
    if hub.is_schengen:
        # Only an entry point if the inbound leg came from outside Schengen.
        return not is_schengen_airport(arriving_from.origin)
    return True   # non-Schengen hubs: always clear immigration to go landside
```

Consequence: BKK → WAW → OSL clears Schengen immigration at Warsaw, not Oslo. The
traveller can leave the airport at WAW with no bag reclaim (single ticket, bags
through-checked) and a short queue. That's one of the best cases in the whole
dataset — surface it prominently.

### 5.3 Bands

| Band | usable_minutes | Plan shape |
|---|---|---|
| `NO_EXIT` | < 180 | Stay airside. Recommend lounge / rest zone. |
| `QUICK` | 180–359 | One anchor activity, near-airport or one metro line. |
| `HALF_DAY` | 360–659 | 2–3 activities + a meal. |
| `FULL_DAY` | 660–1079 | Full itinerary + optional day-use hotel. |
| `OVERNIGHT` | ≥ 1080 | Accommodation required; plan around sleep. |

**A 12-hour layover typically lands in `HALF_DAY`, not `FULL_DAY`.** Worked
example, DXB, 12h gross, single ticket, entry point:

```
720 - 25 (disembark) - 35 (immigration) - 60 (transfer x2)
    - 180 (recheck) - 45 (margin) = 375 → HALF_DAY
```

Six and a quarter hours. This is the headline finding and the product must never
imply otherwise.

### 5.4 Daylight overlay

Clock time matters more than duration. Compute the overlap of the usable window
with local 08:00–21:00.

```python
if daylight_minutes < 120:
    if usable >= 480: band = "OVERNIGHT_NIGHT_ARRIVAL"   # bed, not sightseeing
    else:             band = "NO_EXIT"                    # nothing is open
```

A 22:00–10:00 layover is a hotel opportunity, not a sightseeing one. An
08:00–20:00 layover is the reverse. Same duration, opposite plan.

### 5.5 Score

```python
WEIGHTS = {
    "usable":     0.30,   # normalised against 720 min, capped
    "daylight":   0.20,   # normalised against 480 min, capped
    "access":     0.20,   # 1 - (transfer_minutes / 90), floored at 0
    "entry_ease": 0.20,   # schengen_internal 1.0 | visa_free 0.9 | voa 0.6
                          # | evisa 0.3 | visa_required 0.0
    "density":    0.10,   # hub.activity_density
}
PENALTIES = {
    "bag_reclaim": -15,
    "terminal_change": -5,
}
```

### 5.6 Hard gates

Score is forced to 0 and `blocked_reason` set when any of these hold:

1. `entry_type` is `visa_required` or `no_landside_access` for this passport.
2. `usable_minutes < 180`.
3. Self-transfer with checked bags and `hub.has_left_luggage is False`.
4. Passport validity requirement not met (we can't check this — surface it as a
   user-confirmed checkbox, not a silent pass).

---

## 6. Entry rules — Nordic passports, v0 hubs

All five Nordic passports are treated identically in v0. Verify each cell against
the destination country's official source before launch and stamp `verified_on`.

| Hub | Country | Entry type | Max stay | Passport validity | Immigration at arrival |
|---|---|---|---|---|---|
| WAW | Poland | `schengen_internal` | unlimited | valid | 0 min if from Schengen, 30 if not |
| RIX | Latvia | `schengen_internal` | unlimited | valid | 0 min if from Schengen, 30 if not |
| IST | Turkey | `visa_free` | 90 / 180 days | 150 days | 30 min |
| DOH | Qatar | `visa_free` | 90 days | 90 days | 25 min |
| DXB | UAE | `visa_free` | 90 days | 180 days | 35 min |
| AUH | UAE | `visa_free` | 90 days | 180 days | 35 min |

Note the Turkish passport-validity rule — 150 days, not the usual 90 or 180. It
is a real rejection cause and it is exactly the kind of detail that makes this
product trustworthy or worthless.

**The v0 scope makes this table trivially easy.** That is intentional: prove the
scoring, plan generation and comparison UI first, then take on the hard matrix
(non-EU passports, US CBP, China's visa-free transit windows, UK DATV) in v1
where it becomes the actual moat.

---

## 7. Non-negotiable data discipline

Entry rules and transfer times go stale and being wrong is a missed flight.

- Every `EntryRule` row carries `verified_on`. Rows older than 180 days render
  with a staleness warning in the UI and are excluded from any "recommended" list.
- Every generated plan carries a visible disclaimer: entry requirements are the
  traveller's responsibility and must be confirmed with the carrier.
- Never present a computed `usable_minutes` as a guarantee. Present it as
  "roughly N hours outside the airport" and always show the return-by time.

---

## 8. Fare data

### 8.1 Source abstraction

Fares arrive through one interface with two implementations behind it:

```python
class FareSource(Protocol):
    def search(self, origin: str, dest: str, depart: date) -> list[Itinerary]: ...

class FixtureSource:   # reads fixtures/itineraries/*.json
class AmadeusSource:   # step 8
```

Selected by `FARE_SOURCE=fixture|amadeus`, defaulting to `fixture`. The scoring
layer must not be able to tell which one it got: fixtures are the same parsed
`Itinerary` shape, not raw Amadeus JSON. Steps 5–7 then need no network access
and no API quota, so there is a demoable product before a single Amadeus call is
spent.

### 8.2 Fixtures

Hand-written, not generated. Random data proves nothing. Each file targets a
specific branch of the scoring logic:

| Fixture | Exercises |
|---|---|
| `dxb_12h_halfday` | §5.3 worked example → 375, `HALF_DAY` |
| `waw_inbound_bkk_osl` | Schengen entry point, no bag reclaim, 120 buffer |
| `waw_outbound_osl_bkk` | Not an entry point, but 180 buffer — the §5.1 rule |
| `ist_night_2200_1000` | Daylight gate → not `HALF_DAY` despite 12h gross |
| `doh_150min` | Hard gate → score 0, `blocked_reason` set |
| `rix_22h_overnight` | `OVERNIGHT` band |
| `dxb_selftransfer_bags` | Bag reclaim penalty, `is_single_ticket: False` |
| `ist_negative_saving` | Plan cost > fare saving → `net_saving_eur < 0` |

Each fixture carries its own baseline fare, so the `Comparison` calculation works
end to end. Each also carries an `expected` block — band, usable minutes, score —
and the suite asserts computed output against it. That makes the fixtures the
regression suite, not just demo dressing.

### 8.3 Demo mode must be visible

When `FARE_SOURCE=fixture`, every page renders a persistent banner: **"Demo data
— these fares are not real."** Not a footnote, not a tooltip. Someone will
screenshot this and the fake prices must not travel without the label.

### 8.4 Amadeus

**Source:** Amadeus Self-Service, `GET /v2/shopping/flight-offers`. It returns
`itineraries[].segments[]` with `departure.at` / `arrival.at`, which is everything
the layover computation needs. No scraping.

**Baseline for comparison:** same OD and dates with `nonStop=true`, or if none
exists, the shortest total-duration itinerary. Cheapest of those is
`baseline_price_eur`.

**Call budget — this is a hard constraint.** The free tier allows ~2000 calls per
month. v0 batch:

```
1 origin × 10 destinations × 8 departure dates = 80 calls
+ 80 baseline calls
= 160 calls per weekly refresh ≈ 640/month
```

That fits with headroom. Any change that multiplies this — more origins, daily
refresh, more dates — needs the budget recalculated first. Build a call counter
into the fetch layer from day one and fail loudly at 80% of quota.

---

## 9. Plan generation

For v0, plans are assembled from a hand-curated activity list per hub, not an LLM
and not a live API. Six hubs × ~8 activities = 48 rows. Each activity has:
`name, lat, lon, minutes_needed, cost_eur, opens_local, closes_local, transfer_minutes_from_centre`.

Plan assembly is a greedy fill: sort by (density of interest / time cost), drop
anything whose opening hours don't intersect the usable window, stop when 80% of
usable time is allocated. Leave 20% slack — a plan with no slack is a plan that
makes people miss flights.

An LLM-generated itinerary layer is tempting and is explicitly v1. Curated rows
are testable; generated ones are not.

---

## 10. Build order and verification

```
1. Hub + EntryRule seed data loaded from JSON → verify: 6 hubs, 5 entry rules,
   all fields non-null, `pytest tests/test_seed.py` green.

2. Layover computation from a static itinerary fixture → verify: the DXB worked
   example in §5.3 returns exactly 375 and band HALF_DAY.

3. Entry-point logic → verify: BKK→WAW→OSL marks WAW as entry point;
   OSL→WAW→BKK does not.

4. Scoring + hard gates → verify: a 150-minute layover scores 0 with
   blocked_reason set; a visa_required rule scores 0 regardless of duration.

5. FareSource protocol + FixtureSource + the 8 fixtures of §8.2 → verify: each
   fixture's `expected` block matches computed output.

6. Comparison calculation → verify: ist_negative_saving yields
   net_saving_eur < 0, and the UI says so plainly.

7. Jinja result page → verify: renders all 8 fixtures without error, each with
   band, usable hours, return-by time, plan, comparison and the §8.3 banner.

8. AmadeusSource + call counter → verify: parses a live response into the same
   Itinerary shape, counter increments, refuses to fire at quota.

9. Nightly batch writing to SQLite → verify: one full run completes under the
   call budget and populates results.
```

Do not proceed to step N+1 until step N's verification passes.

---

## 11. Explicitly out of scope for v0

Building any of these is a spec violation, not initiative:

- Hidden-city / skiplagging search
- User accounts, auth, saved trips
- Booking or payment of any kind
- Accommodation APIs (Booking.com, Hostelworld)
- Additional origins, hubs, destinations or passports
- LLM-generated itineraries
- Mobile app, PWA, SPA framework
- Docker, Kubernetes, CI/CD
- Caching layers beyond SQLite
- Multi-currency (EUR only)
- The `last_onward_of_day` penalty removed from §5.1 and §5.5 — v1, and only
  once a hub schedule source exists to compute it from. Do not infer it from a
  `flight-offers` response.
- Randomly generated fixtures, a fixture-generation script, or an admin UI for
  editing them. Eight hand-written JSON files, committed.

---

## 12. Open questions to resolve before step 8

1. Does Amadeus's terms of service permit displaying fares alongside third-party
   activity recommendations? Read them; this affects v1 monetisation.
2. Is `is_single_ticket` reliably derivable from the Amadeus response, or does it
   need inference from `validatingAirlineCodes` and segment carriers? If the
   latter, that's an assumption to surface, not hide.
3. What is the actual willingness to pay? Before step 7, put a landing page up
   with a fake "generate my plan — €7" button and count clicks.
