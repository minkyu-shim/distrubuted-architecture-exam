#MADE USING CODEX AI
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable
from uuid import uuid4

import asyncpg


FLIGHT_DATABASE_URLS = [
    os.getenv("FLIGHT_DATABASE_URL"),
    "postgresql://postgres:postgres@postgres:5432/flight_db",
    "postgresql://postgres:postgres@localhost:5433/flight_db",
    "postgresql://postgres:postgres@localhost:5432/flight_db",
]
HOTEL_DATABASE_URLS = [
    os.getenv("HOTEL_DATABASE_URL"),
    "postgresql://postgres:postgres@postgres:5432/hotel_db",
    "postgresql://postgres:postgres@localhost:5433/hotel_db",
    "postgresql://postgres:postgres@localhost:5432/hotel_db",
]


async def connect_first_available(urls: list[str | None]) -> asyncpg.Connection:
    last_error: Exception | None = None
    for url in urls:
        if not url:
            continue
        try:
            return await asyncpg.connect(url)
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Could not connect to PostgreSQL") from last_error


async def expect_check_violation(label: str, operation: Callable[[], Awaitable[None]]) -> None:
    try:
        await operation()
    except asyncpg.exceptions.CheckViolationError as exc:
        constraint = getattr(exc, "constraint_name", None) or "unknown constraint"
        print(f"OK: {label} rejected by {constraint}")
        return
    raise AssertionError(f"FAIL: {label} was accepted, but the database constraint should reject it")


async def check_flight_constraints() -> None:
    conn = await connect_first_available(FLIGHT_DATABASE_URLS)
    try:
        async def negative_inventory() -> None:
            await conn.execute(
                """
                UPDATE flights
                SET seats_available = -1
                WHERE id = 'FL-ONE-SEAT'
                """
            )

        async def invalid_booking_seats() -> None:
            await conn.execute(
                """
                INSERT INTO flight_bookings (id, trip_id, flight_id, traveler_name, seats, status)
                VALUES ($1, $2, 'FL-ONE-SEAT', 'Constraint Tester', 0, 'CONFIRMED')
                """,
                uuid4(),
                uuid4(),
            )

        print("Flight database constraints")
        await expect_check_violation("negative flight inventory", negative_inventory)
        await expect_check_violation("zero-seat flight booking", invalid_booking_seats)
    finally:
        await conn.close()


async def check_hotel_constraints() -> None:
    conn = await connect_first_available(HOTEL_DATABASE_URLS)
    try:
        async def negative_inventory() -> None:
            await conn.execute(
                """
                UPDATE hotels
                SET rooms_available = -1
                WHERE id = 'HT-ONE-ROOM'
                """
            )

        async def invalid_reservation_rooms() -> None:
            await conn.execute(
                """
                INSERT INTO hotel_reservations
                    (id, trip_id, hotel_id, traveler_name, nights, rooms, status)
                VALUES ($1, $2, 'HT-ONE-ROOM', 'Constraint Tester', 1, 0, 'CONFIRMED')
                """,
                uuid4(),
                uuid4(),
            )

        print("Hotel database constraints")
        await expect_check_violation("negative hotel inventory", negative_inventory)
        await expect_check_violation("zero-room hotel reservation", invalid_reservation_rooms)
    finally:
        await conn.close()


async def main_async() -> None:
    await check_flight_constraints()
    print()
    await check_hotel_constraints()
    print()
    print("A1 PASS: database constraints reject invalid local state.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
