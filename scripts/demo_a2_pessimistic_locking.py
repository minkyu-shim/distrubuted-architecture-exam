#MADE USING CODEX AI
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


ROOT_DIR = Path(__file__).resolve().parents[1]
FLIGHT_URL = os.getenv("FLIGHT_URL", "http://localhost:8001")
HOTEL_URL = os.getenv("HOTEL_URL", "http://localhost:8002")


def pretty(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def reset_inventory_services() -> None:
    with httpx.Client(timeout=10) as client:
        client.post(f"{FLIGHT_URL}/admin/reset").raise_for_status()
        client.post(f"{HOTEL_URL}/admin/reset").raise_for_status()


def require_fixture(
    state: dict[str, Any],
    label: str,
    inventory_key: str,
    resource_id: str,
    available_field: str,
    expected_available: int,
) -> dict[str, Any]:
    resource = next((item for item in state[inventory_key] if item["id"] == resource_id), None)
    if resource is None:
        raise AssertionError(
            f"FAIL: {label} fixture {resource_id!r} was not found after reset. "
            f"Check reset_db()/seed_db() for {inventory_key}."
        )
    if resource[available_field] != expected_available:
        raise AssertionError(
            f"FAIL: {label} fixture {resource_id!r} has {available_field}="
            f"{resource[available_field]}, expected {expected_available} after reset."
        )
    return resource


def require_lock_implementation() -> None:
    flight_code = (ROOT_DIR / "flight_service" / "main.py").read_text()
    hotel_code = (ROOT_DIR / "hotel_service" / "main.py").read_text()
    missing: list[str] = []

    if "FOR UPDATE" not in flight_code or ".transaction(" not in flight_code:
        missing.append("flight_service/main.py")
    if "FOR UPDATE" not in hotel_code or ".transaction(" not in hotel_code:
        missing.append("hotel_service/main.py")

    if missing:
        raise AssertionError(
            "FAIL: pessimistic locking was not found in "
            + ", ".join(missing)
            + ". Expected the booking/reservation read to run inside a transaction with SELECT ... FOR UPDATE."
        )


async def post_flight_booking(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"{FLIGHT_URL}/flights/FL-ONE-SEAT/bookings",
        json={
            "trip_id": str(uuid4()),
            "traveler_name": "Lock Tester",
            "seats": 1,
            "delay_after_check_ms": 500,
            "fail_after_decrement": False,
        },
    )


async def post_hotel_reservation(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"{HOTEL_URL}/hotels/HT-ONE-ROOM/reservations",
        json={
            "trip_id": str(uuid4()),
            "traveler_name": "Lock Tester",
            "nights": 1,
            "rooms": 1,
            "delay_after_check_ms": 500,
            "force_fail": False,
        },
    )


async def run_pair(
    label: str,
    request_factory,
    state_url: str,
    inventory_key: str,
    resource_id: str,
    available_field: str,
    side_effect_key: str,
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        initial_state = (await client.get(state_url)).json()
        require_fixture(
            initial_state,
            label,
            inventory_key,
            resource_id,
            available_field,
            expected_available=1,
        )
        responses = await asyncio.gather(request_factory(client), request_factory(client))
        state = (await client.get(state_url)).json()

    statuses = sorted(response.status_code for response in responses)
    resource = require_fixture(
        state,
        label,
        inventory_key,
        resource_id,
        available_field,
        expected_available=0,
    )
    created_side_effects = [item for item in state[side_effect_key] if item["status"] == "CONFIRMED"]

    print(label)
    print(f"Response statuses: {statuses}")
    print(f"{available_field}: {resource[available_field]}")
    print(f"Confirmed rows: {len(created_side_effects)}")
    print(pretty(state))
    print()

    if statuses != [200, 409]:
        raise AssertionError(f"FAIL: expected one success and one conflict for {label}, got {statuses}")
    if resource[available_field] != 0:
        raise AssertionError(f"FAIL: expected remaining inventory to be 0 for {label}")
    if len(created_side_effects) != 1:
        raise AssertionError(f"FAIL: expected exactly one confirmed row for {label}")


async def main_async() -> None:
    require_lock_implementation()

    print("Resetting flight and hotel services...")
    reset_inventory_services()
    await run_pair(
        "Flight pessimistic lock",
        post_flight_booking,
        f"{FLIGHT_URL}/debug/state",
        "flights",
        "FL-ONE-SEAT",
        "seats_available",
        "flight_bookings",
    )

    reset_inventory_services()
    await run_pair(
        "Hotel pessimistic lock",
        post_hotel_reservation,
        f"{HOTEL_URL}/debug/state",
        "hotels",
        "HT-ONE-ROOM",
        "rooms_available",
        "hotel_reservations",
    )

    print("A2 PASS: concurrent requests serialize and only one reservation is created.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
