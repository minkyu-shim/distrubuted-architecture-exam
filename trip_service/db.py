from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID, uuid4

import asyncpg

pool: asyncpg.Pool | None = None


def database_url() -> str:
    return os.getenv("TRIP_DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/trip_db")


async def connect_with_retry() -> None:
    global pool
    last_error: Exception | None = None
    for _ in range(40):
        try:
            pool = await asyncpg.create_pool(database_url())
            return
        except Exception as exc:  # pragma: no cover - startup race in Docker
            last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError("Could not connect to trip database") from last_error


def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


async def close() -> None:
    if pool is not None:
        await pool.close()


async def init_db() -> None:
    await get_pool().execute(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            traveler_name TEXT NOT NULL,
            flight_id TEXT NOT NULL,
            hotel_id TEXT NOT NULL,
            nights INTEGER NOT NULL,
            status TEXT NOT NULL,
            saga_step TEXT NOT NULL DEFAULT 'PENDING',
            flight_booking_id UUID,
            hotel_reservation_id UUID,
            payment_authorization_id UUID,
            amount_cents INTEGER,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await get_pool().execute(
        """
        CREATE TABLE IF NOT EXISTS saga_logs (
            id UUID PRIMARY KEY,
            trip_id UUID NOT NULL REFERENCES trips(id),
            step TEXT NOT NULL,
            detail TEXT,
            logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await get_pool().execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            trip_id UUID NOT NULL REFERENCES trips(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def reset_db() -> None:
    await get_pool().execute("DELETE FROM saga_logs")
    await get_pool().execute("DELETE FROM idempotency_keys")
    await get_pool().execute("DELETE FROM trips")


"""
store_idempotency_key makes sure that if there is same idempotency key insertion, it will silently skip it
Then get_idempotency_key returns a row, you know a trip was already created for this key.
AKA, you return the existing trip instead of creating a new one
"""

async def get_idempotency_key(key: str) -> dict | None:
    row = await get_pool().fetchrow(
        "SELECT * FROM idempotency_keys WHERE key = $1",
        key,
    )
    return dict(row) if row else None


async def store_idempotency_key(key: str, trip_id: UUID) -> None:
    await get_pool().execute(
        "INSERT INTO idempotency_keys (key, trip_id) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
        # using $1, $2 to prevent SQL injection
        # ON CONFLICT (key) DO NOTHING -> very important
        # key here is the PK, in another word, no two rows can have the same key. Insert duplicate PK would crash
        # If a row with that key already exists, skip the insert instead of crashing
        key,
        trip_id,
    )


async def create_trip(
    *,
    user_id: str,
    traveler_name: str,
    flight_id: str,
    hotel_id: str,
    nights: int,
) -> dict:
    trip_id = uuid4()
    row = await get_pool().fetchrow(
        """
        INSERT INTO trips (id, user_id, traveler_name, flight_id, hotel_id, nights, status, saga_step)
        VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', 'PENDING')
        RETURNING *
        """,
        trip_id,
        user_id,
        traveler_name,
        flight_id,
        hotel_id,
        nights,
    )
    return dict(row)


async def update_trip(trip_id: UUID, **fields: Any) -> dict:
    if not fields:
        row = await get_pool().fetchrow("SELECT * FROM trips WHERE id = $1", trip_id)
        return dict(row)

    names = list(fields)
    assignments = [f"{name} = ${index + 2}" for index, name in enumerate(names)]
    assignments.append("updated_at = now()")
    sql = f"UPDATE trips SET {', '.join(assignments)} WHERE id = $1 RETURNING *"
    row = await get_pool().fetchrow(sql, trip_id, *[fields[name] for name in names])
    return dict(row)


async def log_saga_step(trip_id: UUID, step: str, detail: str | None = None) -> None:
    await get_pool().execute(
        """
        INSERT INTO saga_logs (id, trip_id, step, detail)
        VALUES ($1, $2, $3, $4)
        """,
        uuid4(), trip_id, step, detail,
    )


async def get_trip(trip_id: UUID) -> dict | None:
    row = await get_pool().fetchrow("SELECT * FROM trips WHERE id = $1", trip_id)
    return dict(row) if row else None


async def state() -> dict[str, list[dict]]:
    trips = await get_pool().fetch("SELECT * FROM trips ORDER BY created_at, id")
    logs = await get_pool().fetch("SELECT * FROM saga_logs ORDER BY logged_at")
    return {"trips": [dict(row) for row in trips], "saga_logs": [dict(row) for row in logs]}