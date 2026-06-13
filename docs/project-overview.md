# Project Overview — Trip Booking System

This document explains how the system works end-to-end, what each piece of code does, and exactly what you need to implement for the idempotency key (Category C).

---

## 1. Big picture

Six services run as separate Docker containers. They all share one PostgreSQL instance but each service has **its own database** — they cannot read each other's tables directly.

```
Client (HTTP)
    │
    ▼
trip-service (:8000)   ← orchestrator, owns trip_db
    │
    ├──HTTP──► flight-service (:8001)   owns flight_db
    ├──HTTP──► hotel-service  (:8002)   owns hotel_db
    ├──HTTP──► payment-service(:8003)   owns payment_db
    │
    └──RabbitMQ──► notification-worker  owns notification_db
                         │
                         ▼
               notification-api (:8004)   read-only API on notification_db
```

**Key rule:** `trip-service` is the only entry point for creating a trip. All other services are called by `trip-service` — the client never calls them directly.

---

## 2. What happens when you POST /trips

This is the entire booking flow, step by step.

```
Client → POST /trips  { user_id, traveler_name, flight_id, hotel_id, nights }
```

### Step 1 — Create a pending trip row (`trip_service/main.py:59`)

`trip_service` immediately inserts a row in `trip_db.trips` with `status = 'PENDING'`. This gives the trip a UUID before any downstream calls happen.

```python
trip = await db.create_trip(user_id=..., traveler_name=..., flight_id=..., ...)
trip_id = trip["id"]  # a fresh UUID4
```

### Step 2 — Book a flight (`trip_service/clients.py:39`)

HTTP POST to `flight-service`:
```
POST http://flight-service:8001/flights/{flight_id}/bookings
Body: { trip_id, traveler_name, seats: 1 }
```
`flight-service` checks `seats_available`, decrements it, inserts a row in `flight_bookings`, and returns the booking.
`trip-service` saves the returned `flight_booking_id` in the trip row.

### Step 3 — Reserve a hotel (`trip_service/clients.py:63`)

HTTP POST to `hotel-service`:
```
POST http://hotel-service:8002/hotels/{hotel_id}/reservations
Body: { trip_id, traveler_name, nights, rooms: 1 }
```
Same pattern: check → decrement → insert → return reservation.
`trip-service` saves `hotel_reservation_id`.

### Step 4 — Calculate price and authorize payment (`trip_service/main.py:91`)

`trip-service` fetches flight and hotel objects to get prices, computes the total, then:
```
POST http://payment-service:8003/payments/authorizations
Body: { trip_id, amount_cents }
```
On success, saves `payment_authorization_id` and sets `status = 'CONFIRMED'`.

### Step 5 — Publish event to RabbitMQ (`trip_service/events.py`)

After confirming, `trip-service` publishes a `trip.confirmed` event to the `trip-events` exchange with routing key `trip.confirmed`.

### Step 6 — Worker consumes the event (`notification_worker/worker.py`)

`notification-worker` listens to the `notification-service.trip-confirmed` queue. When it receives the event, it inserts a row in `notification_db.notifications`. The `notification-api` then exposes those rows via `GET /notifications/{trip_id}`.

### Error path

If **any** step from 2 to 4 throws, `trip-service` catches the exception, sets `status = 'FAILED'`, and returns HTTP 502. **No compensation happens** (that's what the B person is fixing).

---

## 3. File map — what each file does

### `trip_service/`
| File | Role |
|---|---|
| `main.py` | FastAPI routes. `POST /trips` is the main one. This is where you add idempotency logic. |
| `db.py` | All SQL for `trip_db`. `init_db()` creates the `trips` table. You will add a second table here. |
| `schemas.py` | Pydantic models for request/response. You will add an `idempotency_key` field to `CreateTripRequest`. |
| `clients.py` | HTTP calls to flight/hotel/payment services. |
| `events.py` | Builds and publishes the RabbitMQ event after confirmation. |
| `pricing.py` | Pure math — calculates total price from flight + hotel prices. |

### `flight_service/`, `hotel_service/`, `payment_service/`
Same layout: `main.py` (routes), `db.py` (SQL), `schemas.py` (Pydantic). You do **not** touch these for the idempotency key — your fix lives entirely in `trip_service`.

### `notification_worker/` + `notification_api/`
The worker (`worker.py`) consumes RabbitMQ messages. The API (`notification_api/main.py`) serves `GET /notifications/{trip_id}`. You do **not** touch these.

### `shared/`
| File | Role |
|---|---|
| `rabbitmq.py` | aio_pika helpers — connect, declare exchange/queue, publish. |
| `faults.py` | `maybe_delay(ms)` — simulates slow services for testing races. |

### `tests/`
| File | Role |
|---|---|
| `test_smoke.py` | Basic happy-path tests. Must keep passing. |
| `test_intentional_flaws.py` | Baseline characterization tests — proves the bugs exist. `test_duplicate_request_is_not_idempotent_in_baseline` is the one you will replace with your idempotency test. |

### `scripts/`
`demo_duplicate_request.py` shows the problem. `common.py` has helpers (`create_trip`, `reset_all`, `base_trip_payload`). You will write a new demo or update this one.

---

## 4. The database schemas that matter to you

### `trip_db.trips` (defined in `trip_service/db.py:42`)
```sql
CREATE TABLE trips (
    id                      UUID PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    traveler_name           TEXT NOT NULL,
    flight_id               TEXT NOT NULL,
    hotel_id                TEXT NOT NULL,
    nights                  INTEGER NOT NULL,
    status                  TEXT NOT NULL,          -- PENDING / CONFIRMED / FAILED
    flight_booking_id       UUID,
    hotel_reservation_id    UUID,
    payment_authorization_id UUID,
    amount_cents            INTEGER,
    error_message           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

You will add a second table to `trip_db` for idempotency keys (see section 5).

### Other DBs (for reference only)
- `flight_db`: `flights` (inventory) + `flight_bookings`
- `hotel_db`: `hotels` (inventory) + `hotel_reservations`
- `payment_db`: `payment_authorizations`
- `notification_db`: `notifications`

---

## 5. The problem you are fixing — duplicate trips

### What goes wrong today

The client submits `POST /trips` with the same logical intent twice (network timeout, user double-click, retry). Each call creates a new trip row with a new UUID, books a new flight seat, reserves a new hotel room, charges payment twice.

```
POST /trips { user_id: "user-1", flight_id: "FL-MANY-SEATS", ... }
→ trip_id: aaaaa, flight_booking_id: bbbbb, payment: $300  ✓

POST /trips { user_id: "user-1", flight_id: "FL-MANY-SEATS", ... }   ← same request again
→ trip_id: ccccc, flight_booking_id: ddddd, payment: $300  ← DUPLICATE
```

The test `test_duplicate_request_is_not_idempotent_in_baseline` (in `test_intentional_flaws.py:48`) already proves this — it asserts two different trip IDs are created.

### What you need to implement

The client sends an idempotency key in the request header or body. `trip-service` stores that key durably in the database. If the same key arrives again, the service returns the **same trip that was created the first time** — no second booking, no second payment.

---

## 6. Implementation outline (what to build, not how)

### A — Add the key to the request schema (`trip_service/schemas.py`)
`CreateTripRequest` needs an optional `idempotency_key: str | None` field. The client generates this (e.g., a UUID they produce before calling).

### B — Add a storage table (`trip_service/db.py`)
A new table in `trip_db` that maps `idempotency_key → trip_id + cached_response`. It must be durable (survives service restarts). Add it to `init_db()` and reset it in `reset_db()`.

### C — Check before creating (`trip_service/main.py`)
At the top of `POST /trips`, before calling `db.create_trip`, check if the key already exists. If it does, return the cached trip immediately. If not, proceed and store the key atomically with the trip creation.

### D — Handle in-flight duplicates
If request A is still processing and request B arrives with the same key, you must decide: return 409 (conflict) or wait. A 409 is simpler to implement and acceptable for this exam.

### E — Update the test (`tests/test_intentional_flaws.py`)
Replace `test_duplicate_request_is_not_idempotent_in_baseline` with a test that sends the same key twice and asserts the same trip ID is returned and only one flight booking was created.

### F — Write a demo script (`scripts/`)
A script that sends the same payload with the same idempotency key twice and prints that only one trip was created.

---

## 7. What the exam grader checks for your concept

From EXAM.md — because idempotency keys have stricter grading:

1. **Persistent key storage** — the key must survive a service restart (i.e., stored in PostgreSQL, not in a dict in memory)
2. **Duplicate-request detection** — same key → same response, no second booking
3. **In-flight handling** — defined behavior when the same key arrives concurrently
4. **Demo/test** — must show the retry/duplicate scenario explicitly

A concept without a test or demo receives limited credit.

---

## 8. Commands to run during development

```bash
# Rebuild and restart after code changes
docker compose up --build -d

# Run all tests
docker compose run --rm tools pytest

# Run only the flaw tests
docker compose run --rm tools pytest tests/test_intentional_flaws.py

# See the duplicate problem in action (baseline)
docker compose run --rm tools python scripts/demo_duplicate_request.py

# Reset all data between runs
docker compose run --rm tools python scripts/reset_all.py

# Full rebuild (needed if you change table definitions)
docker compose down -v && docker compose up --build -d
```

> **Important:** Every time you change `init_db()` to add a new table or column, run `docker compose down -v && docker compose up --build -d` so the existing volume is wiped and the table is created fresh.
