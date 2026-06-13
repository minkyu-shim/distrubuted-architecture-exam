# Category A - Local correctness

This card documents the two Category A concepts implemented in the local inventory services.

---

# A1 - Database constraints

## Category

A - Local correctness, A1 - Integrity and atomicity.

## Problem

The baseline application could create invalid local state if application logic was wrong, bypassed, or interrupted. For inventory services, the important invalid states are negative inventory, zero-sized bookings, invalid reservation sizes, and unsupported booking statuses.

## Invariant or guarantee

The database rejects invalid rows even if the request reaches the database directly:

- flight seats cannot be negative;
- hotel rooms cannot be negative;
- flight booking seats must be greater than zero;
- hotel reservation nights and rooms must be greater than zero;
- booking and reservation status must be either `CONFIRMED` or `CANCELLED`;
- bookings and reservations must reference an existing flight or hotel.

## Modified files

- `flight_service/db.py`
- `hotel_service/db.py`
- `scripts/demo_a1_database_constraints.py`

## Behavior before

Without database constraints, a bug or direct SQL write could store impossible values such as negative available seats, negative available rooms, or a booking with zero seats. The service would then expose invalid state through `/debug/state` and future requests would build on corrupted local data.

## Behavior after

PostgreSQL enforces the local invariants with `CHECK`, `PRIMARY KEY`, and `REFERENCES` constraints. Invalid updates or inserts fail at the database layer instead of being persisted.

## How to test

Run the stack, then run:

```bash
docker compose run --rm tools python scripts/demo_a1_database_constraints.py
```

Expected result: the script attempts invalid writes and prints that PostgreSQL rejected them, then ends with:

```text
A1 PASS: database constraints reject invalid local state.
```

## Limitation

Database constraints protect local table invariants only. They do not by themselves serialize concurrent requests, coordinate state across services, or compensate a distributed booking when another service fails.

---

# A2 - Pessimistic locking

## Category

A - Local correctness, A2 - Concurrency control.

## Problem

The baseline flight and hotel services checked available inventory before decrementing it. Two concurrent requests could both read the same available value, both pass the check, and both create confirmed reservations for a single remaining seat or room.

## Invariant or guarantee

For each flight or hotel inventory row, concurrent reservation attempts are serialized. When only one seat or room is available, two concurrent requests cannot both create confirmed reservations for it.

## Modified files

- `flight_service/main.py`
- `hotel_service/main.py`
- `scripts/demo_a2_pessimistic_locking.py`

## Behavior before

Concurrent requests against a one-seat flight or one-room hotel could both observe inventory as available before either request decremented it. This could create multiple confirmed rows for one unit of inventory and drive inventory below the intended limit.

## Behavior after

The booking and reservation endpoints open a database transaction and read the inventory row with `SELECT ... FOR UPDATE`. PostgreSQL locks that row until the transaction finishes. A second concurrent request waits, then re-checks the updated inventory and returns `409` if no inventory remains.

## How to test

Run the stack, then run:

```bash
docker compose run --rm tools python scripts/demo_a2_pessimistic_locking.py
```

Expected result: for both the flight and hotel cases, one concurrent request succeeds with `200`, the other fails with `409`, remaining inventory is `0`, and exactly one confirmed booking or reservation exists. The script should end with:

```text
A2 PASS: concurrent requests serialize and only one reservation is created.
```

## Limitation

The pessimistic lock protects local inventory rows in the flight and hotel services. It does not make the whole distributed trip workflow atomic, and it can reduce concurrency when many requests target the same flight or hotel row.
