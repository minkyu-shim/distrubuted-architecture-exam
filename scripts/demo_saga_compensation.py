# MADE WITH CLAUDE.IA
"""
Demo: Saga compensation on payment failure.

Shows that when payment is declined after a flight and hotel are successfully
booked, the saga transitions to COMPENSATING and cancels both reservations,
restoring inventory.
"""
from __future__ import annotations

import os
import httpx

TRIP_URL = os.getenv("TRIP_URL", "http://trip-service:8000")
FLIGHT_URL = os.getenv("FLIGHT_URL", "http://flight-service:8000")
HOTEL_URL = os.getenv("HOTEL_URL", "http://hotel-service:8000")


def get(url: str) -> dict:
    r = httpx.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def post(url: str, payload: dict) -> dict:
    r = httpx.post(url, json=payload, timeout=10)
    return r


def print_inventory(label: str) -> None:
    flights = get(f"{FLIGHT_URL}/flights")
    hotels = get(f"{HOTEL_URL}/hotels")
    print(f"\n--- Inventory: {label} ---")
    for f in flights:
        print(f"  Flight {f['id']}: {f['seats_available']} seats available")
    for h in hotels:
        print(f"  Hotel  {h['id']}: {h['rooms_available']} rooms available")


def main() -> None:
    # Reset state
    httpx.post(f"{TRIP_URL}/admin/reset", timeout=10)
    httpx.post(f"{FLIGHT_URL}/admin/reset", timeout=10)
    httpx.post(f"{HOTEL_URL}/admin/reset", timeout=10)

    flights = get(f"{FLIGHT_URL}/flights")
    hotels = get(f"{HOTEL_URL}/hotels")
    flight_id = flights[0]["id"]
    hotel_id = hotels[0]["id"]

    print_inventory("before booking attempt")

    print("\n>>> Attempting trip booking with forced payment decline...")
    resp = post(f"{TRIP_URL}/trips", {
        "user_id": "user-saga-demo",
        "traveler_name": "Saga Tester",
        "flight_id": flight_id,
        "hotel_id": hotel_id,
        "nights": 2,
        "simulate": {
            "payment_force_decline": True,
            "flight_delay_after_check_ms": 0,
            "hotel_delay_after_check_ms": 0,
            "hotel_force_fail": False,
            "payment_force_error": False,
            "payment_delay_ms": 0,
            "publish_event_twice": False,
        },
    })

    print(f"HTTP status: {resp.status_code}")
    body = resp.json()
    trip_id = body.get("detail", {}).get("trip_id")
    print(f"Response: {body}")

    print_inventory("after failed booking (should be unchanged if saga compensated)")

    if trip_id:
        trip_state = get(f"{TRIP_URL}/trips/{trip_id}")
        print(f"\n--- Trip final state ---")
        print(f"  status:    {trip_state['status']}")
        print(f"  saga_step: {trip_state['saga_step']}")

        debug = get(f"{TRIP_URL}/debug/state")
        logs = [l for l in debug.get("saga_logs", []) if l["trip_id"] == trip_id]
        print(f"\n--- Saga log for trip {trip_id} ---")
        for log in logs:
            print(f"  {log['logged_at']}  {log['step']}  {log.get('detail') or ''}")

    print("\nDone.")


if __name__ == "__main__":
    main()