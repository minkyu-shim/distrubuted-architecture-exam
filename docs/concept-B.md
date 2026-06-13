# Saga with a durable state machine

## Category

B — Distributed workflow or messaging

## Problem

The baseline trip booking is a plain sequence of remote calls with no durable
state between steps. If payment fails after a flight and hotel are successfully
reserved, those reservations are left permanently dangling. There is no record
of how far the booking got, and no compensation is triggered.

## Invariant or guarantee

If any step of the booking fails, all previously completed steps are
compensated in reverse order. The final state of every resource
(meaning flight seats and hotel rooms) must be the same as if the booking had never
started. The saga step at the time of failure is recorded.

## Modified files

- `trip_service/db.py` — added `saga_step` column on `trips`, added `saga_logs` table, added `log_saga_step()`
- `trip_service/main.py` — rewrote booking flow as explicit saga steps with compensation path
- `trip_service/clients.py` — added `cancel_flight_booking()` and `cancel_hotel_reservation()`
- `scripts/demo_saga_compensation.py` — demonstration script

## Behavior before

`PENDING → (flight booked) → (hotel reserved) → payment fails → status=FAILED`

Flight and hotel reservations remain in state CONFIRMED. Inventory is not
restored. The system has no record of what was completed before the failure.

## Behavior after

`PENDING → BOOKING_FLIGHT → FLIGHT_BOOKED → RESERVING_HOTEL → HOTEL_RESERVED
→ AUTHORIZING_PAYMENT → COMPENSATING → HOTEL_CANCELLED → FLIGHT_CANCELLED
→ CANCELLED`

Each transition is written to `saga_logs` before the next remote call. If
payment failure, the compensation path cancels botht the hotel then flight in reverse
order. Inventory is restored. The `saga_step` column on the trip row shows
the last durable step reached.

## How to test

```bash
docker compose run --rm tools python scripts/demo_saga_compensation.py
```

Expected output:
- Inventory before and after the failed booking is identical (seats/rooms unchanged)
- Trip `status` is `FAILED`, `saga_step` is `CANCELLED`
- Saga log shows: PENDING → BOOKING_FLIGHT → FLIGHT_BOOKED → RESERVING_HOTEL
  → HOTEL_RESERVED → AUTHORIZING_PAYMENT → COMPENSATING → HOTEL_CANCELLED
  → FLIGHT_CANCELLED

## Limitation

Compensation itself is not retried if it fails. If `cancel_hotel_reservation`
throws, the step is logged as `HOTEL_CANCEL_FAILED` but no further attempt is
made. A production system would need a separate recovery process (like for example a
scheduled job that retries `COMPENSATING` sagas).