from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException

from shared.logging import configure_logging
from trip_service import clients, db, events
from trip_service.pricing import calculate_amount_cents
from trip_service.schemas import CreateTripRequest

SERVICE_NAME = "trip-service"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(SERVICE_NAME)
    await db.connect_with_retry()
    await db.init_db()
    yield
    await db.close()


app = FastAPI(title="Trip Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/admin/reset")
async def reset() -> dict[str, str]:
    await db.reset_db()
    return {"status": "ok"}


@app.get("/debug/state")
async def debug_state() -> dict:
    return await db.state()


@app.get("/trips")
async def list_trips() -> list[dict]:
    return (await db.state())["trips"]


@app.get("/trips/{trip_id}")
async def get_trip(trip_id: UUID) -> dict:
    trip = await db.get_trip(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip


async def _compensate(trip_id: UUID, trip: dict, reason: str) -> dict:
    """
    Compensation path: cancel whatever was successfully booked, in reverse order.
    Each compensation step is logged durably so partial compensation is visible.
    """
    await db.update_trip(trip_id, saga_step="COMPENSATING")
    await db.log_saga_step(trip_id, "COMPENSATING", reason)

    if trip.get("hotel_reservation_id"):
        try:
            await clients.cancel_hotel_reservation(trip["hotel_reservation_id"])
            await db.log_saga_step(trip_id, "HOTEL_CANCELLED")
        except Exception as exc:
            await db.log_saga_step(trip_id, "HOTEL_CANCEL_FAILED", str(exc))
            logging.exception(f"Failed to cancel hotel reservation {trip["hotel_reservation_id"]}")

    if trip.get("flight_booking_id"):
        try:
            await clients.cancel_flight_booking(trip["flight_booking_id"])
            await db.log_saga_step(trip_id, "FLIGHT_CANCELLED")
        except Exception as exc:
            await db.log_saga_step(trip_id, "FLIGHT_CANCEL_FAILED", str(exc))
            logging.exception(f"Failed to cancel flight booking {trip["flight_booking_id"]}")

    return await db.update_trip(trip_id, saga_step="CANCELLED", status="FAILED", error_message=reason)


@app.post("/trips")
async def create_trip(request: CreateTripRequest) -> dict:
    if request.idempotency_key:
        existing = await db.get_idempotency_key(request.idempotency_key)
        if existing:
            trip = await db.get_trip(existing["trip_id"])
            if trip is None:
                # Theoretically shouldn't happen, unless someone manually deletes a trip row in the DB
                raise HTTPException(status_code=500, detail="Idempotency key points to missing trip")
            return trip

    trip = await db.create_trip(
        user_id=request.user_id,
        traveler_name=request.traveler_name,
        flight_id=request.flight_id,
        hotel_id=request.hotel_id,
        nights=request.nights,
    )
    trip_id = trip["id"]
    await db.log_saga_step(trip_id, "PENDING")
    if request.idempotency_key:
        await db.store_idempotency_key(request.idempotency_key, trip_id)

    # First step: Book flight
    await db.update_trip(trip_id, saga_step="BOOKING_FLIGHT")
    await db.log_saga_step(trip_id, "BOOKING_FLIGHT")

    try:
        flight_booking = await clients.book_flight(
            flight_id=request.flight_id,
            trip_id=str(trip_id),
            traveler_name=request.traveler_name,
            delay_after_check_ms=request.simulate.flight_delay_after_check_ms,
        )
    except Exception as err:
        compensated = await _compensate(trip_id, trip, str(err))
        raise HTTPException(status_code=502, detail={"trip_id": str(trip_id), "error": compensated["error_message"]})

    trip = await db.update_trip(
        trip_id,
        saga_step="FLIGHT_BOOKED",
        flight_booking_id=UUID(flight_booking["id"]),
    )
    await db.log_saga_step(trip_id, "FLIGHT_BOOKED", flight_booking["id"])

    # Second step: Reserve hotel
    await db.update_trip(trip_id, saga_step="RESERVING_HOTEL")
    await db.log_saga_step(trip_id, "RESERVING_HOTEL")
    try:
        hotel_reservation = await clients.reserve_hotel(
            hotel_id=request.hotel_id,
            trip_id=str(trip_id),
            traveler_name=request.traveler_name,
            nights=request.nights,
            delay_after_check_ms=request.simulate.hotel_delay_after_check_ms,
            force_fail=request.simulate.hotel_force_fail,
        )
    except Exception as err:
        compensated = await _compensate(trip_id, trip, str(err))
        raise HTTPException(status_code=502, detail={"trip_id": str(trip_id), "error": compensated["error_message"]})

    trip = await db.update_trip(trip_id, saga_step="HOTEL_RESERVED", hotel_reservation_id=UUID(hotel_reservation["id"]))
    await db.log_saga_step(trip_id, "HOTEL_RESERVED", hotel_reservation["id"])

    # Third step: Compute price
    flight = await clients.get_flight(request.flight_id)
    hotel = await clients.get_hotel(request.hotel_id)
    amount_cents = calculate_amount_cents(
        flight_price_cents=flight["price_cents"],
        hotel_price_per_night_cents=hotel["price_per_night_cents"],
        nights=request.nights,
    )
    trip = await db.update_trip(trip_id, amount_cents=amount_cents)

    # Fourht step: Authorize payment
    await db.update_trip(trip_id, saga_step="AUTHORIZING_PAYMENT")
    await db.log_saga_step(trip_id, "AUTHORIZING_PAYMENT")
    try:
        payment = await clients.authorize_payment(
            trip_id=str(trip_id),
            amount_cents=amount_cents,
            force_decline=request.simulate.payment_force_decline,
            force_error=request.simulate.payment_force_error,
            delay_ms=request.simulate.payment_delay_ms,
        )
    except Exception as err:
        compensated = await _compensate(trip_id, trip, str(err))
        raise HTTPException(status_code=502, detail={"trip_id": str(trip_id), "error": compensated["error_message"]})

    trip = await db.update_trip(
        trip_id,
        saga_step="CONFIRMED",
        payment_authorization_id=UUID(payment["id"]),
        status="CONFIRMED",
        error_message=None,
    )
    await db.log_saga_step(trip_id, "CONFIRMED")

    try:
        await events.publish_confirmation(trip, publish_twice=request.simulate.publish_event_twice)
    except Exception:
        logging.exception("Failed to publish trip.confirmed event")

    return trip