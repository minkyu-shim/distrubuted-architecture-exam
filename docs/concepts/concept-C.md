# Idempotency Key

## Category

C

## Problem

`POST /trips` in the naive application has no deduplication mechanism. If a client submits the same booking request twice (network timeout, user double-click, automatic retry), the system treats each request as a new trip. Both requests succeed, creating two separate trips with two flight bookings, two hotel reservations, and two payment authorizations for the same logical intent.

## Invariant or guarantee

For a given idempotency key, at most one trip is created. Any subsequent request carrying the same key returns the trip created by the first request — no new downstream calls are made to flight-service, hotel-service, or payment-service.

## Modified files

- `trip_service/schemas.py` — added `idempotency_key: str` field to `CreateTripRequest`
- `trip_service/db.py` — added `idempotency_keys` table, `get_idempotency_key`, and `store_idempotency_key` functions
- `trip_service/main.py` — added duplicate check at the top of `POST /trips` and key storage after trip creation
- `tests/test_intentional_flaws.py` — replaced baseline characterization test with idempotency correctness test
- `tests/test_smoke.py` — updated happy-path test to include required idempotency key
- `scripts/demo_idempotency_key.py` — new demo script

## Behavior before

Two identical requests to `POST /trips` both received HTTP 200 with different trip IDs. The flight, hotel, and payment services were each called twice, creating duplicate records across all databases.

## Behavior after

The second request carrying the same idempotency key receives HTTP 200 with the same trip ID as the first. No calls are made to flight-service, hotel-service, or payment-service. Only one row exists in each downstream service's database.

## How to test

Demo script — shows both responses are identical and that only one record exists in each service:

```bash
docker compose run --rm tools python scripts/demo_idempotency_key.py
```

Automated test — sends the same key twice and asserts one trip, one flight booking, one hotel reservation, one payment:

```bash
docker compose run --rm tools pytest tests/test_intentional_flaws.py::test_duplicate_request_with_same_idempotency_key_creates_one_trip -v
```

## Limitation

There is a narrow race condition: if two requests with the same key arrive simultaneously, both may pass the initial key lookup before either has stored the key. In that case, two trips can be created. The `ON CONFLICT DO NOTHING` on the key insert means only one key row survives, but the second trip row is orphaned in the database. Closing this race completely would require wrapping `create_trip` and `store_idempotency_key` in a single database transaction.

Pessimistic locking (SELECT FOR UPDATE), as implemented in the flight and hotel services for the overbooking problem, does not help here. That technique locks an existing row so that concurrent reads block until the first writer commits. In this race, however, there is no existing row to lock at check time — the `idempotency_keys` table is empty for this key when both requests arrive. There is nothing to lock, so the race cannot be prevented by a row-level lock alone.
