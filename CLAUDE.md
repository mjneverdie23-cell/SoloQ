# CLAUDE.md

Instructions for Claude Code working in this repository. Read `SPEC.md` before
writing any code. `SPEC.md` is the source of truth; this file is how to work.

---

## What we're building

A search tool that finds cheap round-trip fares containing a long layover, scores
how much of that layover is actually usable outside the airport, and generates a
plan for it. Full detail in `SPEC.md`.

The single most important domain fact: **a 12-hour layover is about 6 usable
hours**, not 12. Deductions for disembarking, immigration, two transfers and the
re-check buffer eat half of it. Any code or copy that implies otherwise is a bug.

---

## How to work in this repo

### Think before coding
- State assumptions explicitly. If a spec detail is ambiguous, stop and ask —
  don't pick silently and don't hide the confusion in a comment.
- If you see a simpler approach than what `SPEC.md` describes, say so before
  implementing. The spec is a strong default, not scripture.
- If multiple interpretations exist, present them.

### Simplicity first
- Write the minimum code that satisfies the current step. Nothing speculative.
- No abstractions for single-use code. No config options nobody asked for. No
  error handling for scenarios that can't occur.
- If you write 200 lines where 50 would do, rewrite it.
- Test: would a senior engineer call this overcomplicated? If yes, simplify.

### Surgical changes
- Touch only what the current task requires.
- Don't improve adjacent code, comments or formatting.
- Don't refactor working code.
- Match existing style even where you'd choose differently.
- If you spot unrelated dead code, mention it — don't delete it.
- Remove imports and variables that *your* change orphaned. Nothing else.

### Goal-driven execution
Work step by step through `SPEC.md` §10. Each step has a stated verification.
**Do not start step N+1 until step N's verification passes.** Before any
multi-step task, state the plan as:

```
1. [step] → verify: [check]
2. [step] → verify: [check]
```

If a check fails, fix it before moving on. Don't accumulate broken steps.

---

## Hard rules

1. **Scope is locked.** `SPEC.md` §11 lists what is out of scope. Building any of
   it is a spec violation, not initiative. That includes hidden-city search, auth,
   booking, Docker, and any additional hub, origin or passport.

2. **Never guess reference data.** Hub transfer times, immigration durations and
   entry rules come from `hubs.seed.json` and are marked unverified. If you need a
   value that isn't there, add it to the seed file with `"verified_on": null` and
   flag it in your response. Do not invent a plausible number and move on.

3. **Conservatism in time maths is mandatory.** Every rounding decision in
   `usable_minutes` rounds *down*. Being wrong here means a real person misses a
   real flight. When in doubt, subtract more.

4. **Watch the API quota.** The Amadeus free tier is ~2000 calls/month and the v0
   budget is ~640. Build the call counter in step 5 before any batch code exists.
   Any change that multiplies call volume needs the budget recalculated first —
   raise it, don't absorb it.

5. **No scraping.** Amadeus API only. Scraping is what generated the legal
   exposure for comparable products.

6. **Timezones are always explicit.** Every datetime is tz-aware and localised to
   the airport it describes. A naive datetime anywhere in this codebase is a bug.
   The daylight-overlap calculation is meaningless without this.

---

## Test fixtures that must always pass

These encode the domain logic. If a change breaks one, the change is wrong until
proven otherwise.

```
test_dxb_12h_halfday        DXB, 12h gross, single ticket, entry point
                            → usable == 390, band == "HALF_DAY"

test_schengen_entry_inbound BKK→WAW→OSL → WAW.is_entry_point == True
test_schengen_entry_outbound OSL→WAW→BKK → WAW.is_entry_point == False

test_short_layover_gate     150 min gross → score == 0,
                            blocked_reason is not None

test_night_arrival          layover 22:00–10:00 local, 12h gross
                            → daylight_minutes < 120,
                              band != "HALF_DAY"

test_negative_saving        plan cost > fare saving
                            → net_saving_eur < 0 and surfaced in output
```

---

## Commands

```bash
uv sync                      # install
uv run pytest                # all tests
uv run pytest tests/test_layover.py -v
uv run python -m app.batch   # nightly fetch (respects quota counter)
uv run fastapi dev app/main.py
```

---

## First task

Step 1 only: load `hubs.seed.json` into SQLite via a small schema matching the
dataclasses in `SPEC.md` §4, and write `tests/test_seed.py` verifying 6 hubs and
5 entry rules load with all non-nullable fields populated.

Nothing else. No scoring, no API client, no frontend. Show me the plan first.
