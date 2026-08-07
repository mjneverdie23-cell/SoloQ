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
class Airport:
    iata: str                      # "IST"
    city: str
    country_iso2: str
    is_schengen: bool
    tz_name: str                   # IANA zone, e.g. "Europe/Warsaw"
    verified_on: date | None       # membership changes — see §7

@dataclass
class Hub:                         # keyed on iata, FK to airport
    iata: str                      # "IST"
    transfer_minutes: int          # airport -> city centre, one way, public transit
    transfer_cost_eur: float       # one way
    transfer_mode: str             # "metro" | "train" | "bus"
    transfer_note: str | None      # operational caveats, free text
    disembark_minutes: int         # wheels-down to landside door, excl. immigration
    immigration_minutes: int       # only applied when this is an entry point
    recheck_buffer_minutes: int    # required presence before onward departure
    has_left_luggage: bool
    activity_density: float        # 0.0-1.0, hand-scored editorial judgement
    verified_on: date | None       # transfer/buffer times go stale — see §7

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
    gross_minutes: int             # @property off arrival/departure, never stored
    usable_minutes: int            # after all deductions, §5
    open_hours_minutes: int        # overlap of city window with local 08:00-21:00
    is_entry_point: bool           # do we clear immigration here?
    requires_bag_reclaim: bool
    requires_terminal_change: bool # penalised in §5.5
    band: str                      # NO_EXIT | QUICK | HALF_DAY | FULL_DAY
                                   # | OVERNIGHT | OVERNIGHT_NIGHT_ARRIVAL (§5.4)
    score: int                     # 0-100
    blocked_reasons: list[str]     # every failing gate, not just the first

@dataclass
class Comparison:
    itinerary_price_eur: float
    baseline_price_eur: float      # cheapest fast itinerary, same OD + dates
    layover_plan_cost_eur: float   # transfers + activities + stay
    itinerary_duration_minutes: int
    baseline_duration_minutes: int
    fare_saving_eur: float         # @property, baseline - itinerary
    net_saving_eur: float          # @property, fare_saving - plan_cost
    extra_hours: float             # @property, duration difference / 60
```

`net_saving_eur` is the number the whole product exists to display. If it's
negative the itinerary is honestly worse and we say so.

All three headline figures are derived from the five stored ones, so they are
properties (hard rule 7). `extra_hours` needs `baseline_duration_minutes`,
which the fixtures carry alongside `baseline_price_eur` as a hand-set
placeholder; the real one comes from Amadeus at step 9.

A hub is an airport with extra operational data. `city`, `country_iso2` and
`is_schengen` are airport facts and live only on `Airport` — duplicating them on
`Hub` is the same error class as the removed `exit_control_minutes`. There is no
country table: Schengen membership is properly a country property, but at
fifteen countries against seventeen airports the normalisation buys nothing and
costs a join. Revisit past ~50 airports.

---

## 5. Scoring — the core logic

### 5.1 Usable time

```python
def recheck_buffer(hub: Hub, onward: Segment, schengen_airports: frozenset[str]) -> int:
    # A Schengen hub's 120 assumes an intra-Schengen departure. Leaving the zone
    # means full exit control — which is what the non-Schengen 180 already covers.
    if hub.iata in schengen_airports and onward.destination not in schengen_airports:
        return 180
    return hub.recheck_buffer_minutes


def city_window(
    lay: Layover, hub: Hub, onward: Segment, schengen_airports: frozenset[str]
) -> tuple[datetime, datetime]:
    start = lay.arrival + timedelta(minutes=(
        hub.disembark_minutes
        + (hub.immigration_minutes if lay.is_entry_point else 0)
        + (30 if lay.requires_bag_reclaim else 0)
        + hub.transfer_minutes
    ))
    end = lay.departure - timedelta(minutes=(
        recheck_buffer(hub, onward, schengen_airports)
        + hub.transfer_minutes
        + SAFETY_MARGIN               # protects the return, not the arrival
    ))
    return start, end


def usable_minutes(
    lay: Layover, hub: Hub, onward: Segment, schengen_airports: frozenset[str]
) -> int:
    start, end = city_window(lay, hub, onward, schengen_airports)
    return max(0, floor_minutes(end - start))
```

`usable_minutes` is *derived* from the window, not computed alongside it. The two
are algebraically identical — `end - start` expands to exactly the old list of
deductions — and computing them separately is how `exit_control_minutes` and
`recheck_buffer_minutes` came to disagree. The equality is asserted in the test
suite so a future edit to one cannot silently diverge from the other.

The endpoints are also the product. *"Leave the airport around 23:55, be back by
05:15"* is the sentence a traveller acts on; `usable_minutes` is an internal
scoring number.

The Schengen airport set is passed in, not read from a module-level lookup. It
comes from `load_schengen_airports(conn)` in `app/geo.py`, called once at the
edge; a global that reaches into the database would make these pure functions
impure and awkward to test. It is also the single authority on whether the *hub*
is in Schengen, which is why `Hub` no longer carries `is_schengen`.

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
def is_entry_point(hub: Hub, arriving_from: Segment, schengen_airports: frozenset[str]) -> bool:
    if hub.iata in schengen_airports:
        # Only an entry point if the inbound leg came from outside Schengen.
        return arriving_from.origin not in schengen_airports
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

§5.4 can override any of these with a sixth value, `OVERNIGHT_NIGHT_ARRIVAL`.

**`OVERNIGHT` needs almost a full day of gross layover.** The floor is 1080
*usable* minutes and every hub spends its deductions first, so the gross figure
required is 1080 plus that hub's deductions: RIX spends 257, so `OVERNIGHT`
starts at 22h17m; DXB spends 345, so it starts at 23h45m. A band named for a
night that a 22-hour layover does not reach is exactly the kind of thing to
write down before someone designs a booking flow around the word.

**A 12-hour layover typically lands in `HALF_DAY`, not `FULL_DAY`.** Worked
example, DXB, 12h gross, single ticket, entry point:

```
720 - 25 (disembark) - 35 (immigration) - 60 (transfer x2)
    - 180 (recheck) - 45 (margin) = 375 → HALF_DAY
```

Six and a quarter hours. This is the headline finding and the product must never
imply otherwise.

### 5.4 Open-hours overlay

Clock time matters more than duration. Compute the overlap of the city window
(§5.1) with local `CITY_OPEN_LOCAL`–`CITY_CLOSE_LOCAL`, flat at 08:00–21:00.

```python
if open_hours_minutes < 120:
    if usable >= 480: band = "OVERNIGHT_NIGHT_ARRIVAL"   # bed, not sightseeing
    else:             band = "NO_EXIT"                    # nothing is open
```

This is *open hours*, not daylight, and the distinction is why the flat constant
is defensible. Riga in December gets sunlight roughly 09:00–15:30, and RIX and
WAW are a third of the hubs — as a daylight model 08:00–21:00 would simply be
wrong. But museums keep the same hours in December, and "when the city is awake
and things are open" is what the rule actually means. It is a coarse pre-filter;
§9's per-activity `opens_local` / `closes_local` refines it at step 10. No solar
calculation, no per-hub variation, no timezone table — the `Layover` endpoints
are tz-aware and localised to the hub, so the local wall-clock hour is already
on the datetime.

A 22:00–10:00 layover is a hotel opportunity, not a sightseeing one. An
08:00–20:00 layover is the reverse. Same duration, opposite plan.

The overlap takes the airport's IANA zone, from `Airport.tz_name`. It is not
read off the segment datetimes: `datetime.fromisoformat` yields a fixed UTC
offset, and a fixed offset cannot say what 08:00 local is on a day the offset
changed. Reconstructing day boundaries from one was wrong across a DST
transition by up to an hour, in the direction that overstates open hours and
un-gates a layover with nothing open — 152 windows in a sweep of the 2026
transitions crossed the 120-minute floor because of it.

Still no timezone *table*: one column on the existing `airport` row, carrying
the same `verified_on` discipline as everything else there. Hard rule 6 says
datetimes are "localised to the airport they describe", and an offset is not a
locale.

### 5.5 Score

```python
WEIGHTS = {
    "usable":     0.30,   # normalised against 720 min, capped
    "open_hours": 0.20,   # normalised against 480 min, capped
    "access":     0.20,   # 1 - (transfer_minutes / 90), floored at 0
    "entry_ease": 0.20,   # schengen_internal 1.0 | visa_free 0.9 | voa 0.6
                          # | evisa 0.3 | visa_required 0.0
    "density":    0.10,   # hub.activity_density — editorial, not measured
}
PENALTIES = {
    "bag_reclaim": -15,
    "terminal_change": -5,
}
```

`entry_ease` is nearly inert in v0 and must not be tuned away. All five entry
rules are `schengen_internal` or `visa_free`, so the term only ever takes 1.0 or
0.9 — 20% of the weight moving a maximum of two points — and `visa_required` →
0.0 is unreachable, because it is also a hard gate. It becomes the most
discriminating term in the model the moment v1 adds a non-Nordic passport, and
reweighting now means reweighting back later. Keep the weight and the mapping;
just do not calibrate the other weights against a signal that does not move.

### 5.6 Hard gates

Score is forced to 0 and every failing gate is appended to `blocked_reasons`:

1. `entry_type` is `visa_required` or `no_landside_access` for this passport.
2. `band == "NO_EXIT"`.
3. Self-transfer with checked bags and `hub.has_left_luggage is False`.

Gate 2 reads the band. `usable_minutes < 180` was only ever a proxy for
`NO_EXIT`: §5.3 already turns that duration into `NO_EXIT`, so the band
subsumes the old condition and picks up the open-hours case the proxy missed —
which is how a 22:00 arrival at IST scored 47 while the band knew nothing was
open. Two code paths deciding "is this usable" from the same inputs is hard
rule 7's family. The band computes first; it reads `usable_minutes` and
`open_hours_minutes`, neither of which reads a gate, so there is no cycle.

`NO_EXIT` has two causes and they are different facts, so both are named when
both hold: "layover shorter than 3 usable hours" and "nothing open during the
usable window". `OVERNIGHT_NIGHT_ARRIVAL` also has no open hours and is
deliberately **not** gated — a bed is still a plan.

**Collect all of them; never short-circuit.** Someone told only "layover too
short" will go and find a longer one, then hit the visa wall they were never
shown. That is why `blocked_reasons` is a list.

Passport validity is **not** a gate. We cannot check it, so zeroing a score on
it would imply that we had. It is a user-confirmed checkbox at step 8.

**A blocked layover is not a dropped itinerary.** The flight is still real and
still cheap; it is the city trip that is blocked. The fare saving and the
`Comparison` still render, and §5.3's `NO_EXIT` plan — lounge, rest zone — is
still the output. Step 8 must group these under "no city trip possible" rather
than filtering them out of the results.

### 5.7 v0 derivation rules

Two `Layover` fields are not given by any fare response and are not lookups.
Both are stated here so they are applied mechanically rather than set by feel
per fixture:

- `requires_bag_reclaim` is `not itin.is_single_ticket`. Self-transfer means
  bags are not through-checked, so they come off the belt.
- `requires_terminal_change` is `True` for self-transfer itineraries at
  multi-terminal hubs — DXB, IST, AUH — and `False` otherwise. Single-ticket
  connections are assumed same-terminal or airside-connected.

Terminal change is properly a property of the *pair* of terminals, which
depends on the two carriers, which is data we do not have. This is a heuristic.
A terminal map is v1, and when it arrives the multi-terminal flag moves onto
`Airport` and stops being a constant in code.

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
- **The staleness gate fails closed.** `verified_on = null` means stale, not
  "no requirement". Every row in the seed is null today, so every result carries
  the warning and nothing is eligible for a "recommended" list. That is correct
  and must not be softened: an unverified-data path that renders clean is
  precisely what this section exists to prevent.
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
| `ist_night_2200_1000` | Open-hours gate → `NO_EXIT` despite 12h gross |
| `doh_150min` | Hard gate → score 0, `blocked_reasons` non-empty |
| `rix_23h_overnight` | `OVERNIGHT` band — 22h does not reach it, see below |
| `dxb_selftransfer_bags` | Both penalties — self-transfer at a multi-terminal hub (§5.7) |
| `ist_negative_saving` | Plan cost > fare saving → `net_saving_eur < 0` |

`rix_23h_overnight` is not a typo. RIX deducts 257 minutes, so a 22-hour layover
yields 1063 usable — `FULL_DAY`, seventeen minutes short of the `OVERNIGHT`
floor. It takes 23 hours to reach a band whose name implies a night. That is the
§5.3 lesson at a larger scale and the fixture is named for the truth.

Each fixture carries its own baseline fare and a hard-coded
`layover_plan_cost_eur`, so the `Comparison` calculation works end to end at
step 7 without waiting for §9's activity rows, which are step 10. Each holds
exactly one layover, so its `expected` block is unambiguous. Each also carries an `expected` block — band, usable minutes, score —
and the suite asserts computed output against it. The block carries the **city
window endpoints**, not only durations: moving `SAFETY_MARGIN` from the end of
the window to the start leaves `usable_minutes` byte-identical while shifting
both displayed timestamps by 45 minutes, and only an assertion on the timestamps
catches it. That makes the fixtures the regression suite, not just demo
dressing.

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
1. Airport + Hub + EntryRule seed data loaded from JSON → verify: 17 airports,
   6 hubs, 5 entry rules, all fields non-null, `pytest tests/test_seed.py` green.

2. Layover computation from a static itinerary fixture → verify: the DXB worked
   example in §5.3 returns exactly 375 and band HALF_DAY.

3. Entry-point logic → verify: BKK→WAW→OSL marks WAW as entry point;
   OSL→WAW→BKK does not.

4. Open-hours overlay (§5.4) → verify: ist_night_2200_1000, a layover
   22:00–10:00 local with 12h gross, gives a city window of 23:55–05:15,
   usable 320, open_hours_minutes 0, band NO_EXIT.

5. Scoring + hard gates → verify: a 150-minute layover scores 0 with
   blocked_reasons non-empty; a visa_required rule scores 0 regardless of
   duration; both failing at once produce two reasons, not one; every row
   being unverified makes every result stale and nothing recommendable.

6. FareSource protocol + FixtureSource + the 8 fixtures of §8.2 → verify: each
   fixture's `expected` block matches computed output.

7. Comparison calculation → verify: ist_negative_saving yields
   net_saving_eur < 0, and the UI says so plainly.

8. Jinja result page → verify: renders all 8 fixtures without error, each with
   band, usable hours, return-by time, plan, comparison and the §8.3 banner.

9. AmadeusSource + call counter → verify: parses a live response into the same
   Itinerary shape, counter increments, refuses to fire at quota.

10. Nightly batch writing to SQLite → verify: one full run completes under the
    call budget and populates results.

11. Itinerary import (§13) → verify: the bgo_alg_out_of_scope fixture parses,
    yields user_filtered_stops [1, 2], carries no ucs, and lands in the
    no_hub_data state rather than crashing or scoring an unknown hub.
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
- Any HTTP fetch of a third-party travel page, a headless browser, a PNR lookup
  against a GDS, or airline account integration. See §13.2.

---

## 12. Open questions to resolve before step 9

1. Does Amadeus's terms of service permit displaying fares alongside third-party
   activity recommendations? Read them; this affects v1 monetisation.
2. Is `is_single_ticket` reliably derivable from the Amadeus response, or does it
   need inference from `validatingAirlineCodes` and segment carriers? If the
   latter, that's an assumption to surface, not hide.
3. What is the actual willingness to pay? Before step 8, put a landing page up
   with a fake "generate my plan — €7" button and count clicks.

---

## 13. Itinerary import — step 11

How a traveller gets their trip into the tool. Three input modes, in descending
order of fidelity:

| Mode | Input | Fidelity |
|---|---|---|
| A | Flight numbers + date (`EK146 2026-09-15`) | Exact |
| B | Pasted booking confirmation text | Exact if parseable |
| C | Metasearch URL | OD + dates only |

### 13.1 Mode C is a search query, not an itinerary

A metasearch URL encodes a *search*, not a booking. The specific flight the user
clicked lives in a session on the provider's server and is not in the link. Any
UI that implies otherwise is lying.

So: parse the URL for origin, destination and dates, run our own
`FareSource.search()` on those parameters, and let the user pick from our
results. The copy is **"We found these options for your route"** — never "here's
your trip."

### 13.2 Hard constraints

- **No HTTP request to momondo, kayak, cheapflights, Skyscanner, Google Flights
  or any other metasearch. Ever.** URL string parsing only. Fetching those pages
  is scraping — hard rule 5 — and it is the conduct behind the Skiplagged
  judgment. This holds for every provider format added later.
- Parser failure falls back to the manual form (Mode A). **Never to a guess.**
- No headless browser, no PNR lookup against a GDS, no airline account
  integration. See §11.

### 13.3 URL shapes

Validated against a real link:

```
https://www.momondo.no/flight-search/BGO-ALG/2026-11-02/2026-11-06?ucs=mol85q&sort=bestflight_a&fs=stops%3D1%2C2#dialog
```

- **Host:** match `momondo\.[a-z.]{2,6}`. Real Norwegian links are `momondo.no`,
  not `.com` — the earlier assumption was wrong. Do not enumerate ccTLDs;
  validate on **path shape**, which is the reliable signal. `kayak` and
  `cheapflights` share this format.
- **Path:** `/flight-search/{ORIGIN}-{DEST}/{DEPART}/{RETURN}`. `{RETURN}` is
  absent for a one-way. More than two dates, or more than one OD pair, is
  multi-city: **detect and reject**, do not mis-parse into a round trip.
- **Query:**
  - `fs=stops=N,M` → `user_filtered_stops: list[int]`. URL-decode `%3D`→`=` and
    `%2C`→`,` first. A user who has excluded nonstops has self-qualified as our
    audience — surface that in the result copy.
  - `sort` → ignore.
  - `ucs` → a session correlator. **Stripped, never stored, never logged, never
    echoed back.** `ParsedRoute` has no field for it, so it cannot be persisted
    by forgetting to strip it — absence is structural, not a step in a process.

```python
@dataclass
class ParsedRoute:
    origin: str
    destination: str
    depart: date
    return_date: date | None       # None => one-way
    user_filtered_stops: list[int]
    provider: str                  # "momondo" | "kayak" | "cheapflights" | ...
```

### 13.4 `no_hub_data` — the third result state

The BGO→ALG link above parses cleanly and then has nowhere to go: BGO is not a
v0 origin, ALG is not a v0 destination, and its plausible hubs (CDG, AMS, CPH,
BCN) are not in the hub seed.

That needs a state of its own, distinct from both **blocked** ("you cannot do
this") and **stale** ("we do not trust our own numbers"):

> **`no_hub_data`** — we parsed the route, and we do not have layover data for
> its connecting airports.

- Never crash.
- Never silently drop the layover.
- **Never score an unknown hub with default values.** Unknown is unknown, not
  average. A hub with invented transfer and immigration times would produce a
  confident number with nothing behind it, which is the failure §7 exists to
  prevent, arriving through a different door.

Copy: *"We can parse this route but we don't have layover data for its
connecting airports yet."*

This is the state a stranger hits first — v0 covers one origin and ten
destinations, so almost every pasted link lands here. It deserves better
handling than the paths a stranger reaches only after getting lucky.

### 13.5 Demo behaviour

With `FARE_SOURCE=fixture`, match a parsed route to the nearest fixture and
render it, with the §8.3 banner still on. Paste-a-link is then demoable with
zero network and zero quota, like everything else in steps 6–8.

### 13.6 Fixture

`bgo_alg_out_of_scope` carries the real URL above and asserts: the parse
succeeds, `user_filtered_stops == [1, 2]`, `ucs` is absent from the parsed
object, and the result state is `no_hub_data`.
