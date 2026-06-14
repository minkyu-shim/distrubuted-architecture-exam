from common import base_trip_payload, create_trip, get_state, pretty, reset_all


def main() -> None:
    reset_all()
    payload = base_trip_payload()
    payload["idempotency_key"] = "demo-idem-key-001"

    first = create_trip(payload)
    second = create_trip(payload)

    first_data = first.json()
    second_data = second.json()

    print("=== Idempotency Key Demo ===")
    print()
    print("Same payload with the same idempotency key submitted twice.")
    print()
    print("First response:")
    print(pretty(first_data))
    print()
    print("Second response:")
    print(pretty(second_data))
    print()

    same_id = first_data["id"] == second_data["id"]
    state = get_state()
    trip_count = len(state["trip-service"]["trips"])
    flight_count = len(state["flight-service"]["flight_bookings"])
    hotel_count = len(state["hotel-service"]["hotel_reservations"])
    payment_count = len(state["payment-service"]["payment_authorizations"])

    print("=== Result ===")
    print(f"Same trip ID returned:     {'YES' if same_id else 'NO'}")
    print(f"Trips created:             {trip_count} (expected 1)")
    print(f"Flight bookings created:   {flight_count} (expected 1)")
    print(f"Hotel reservations created:{hotel_count} (expected 1)")
    print(f"Payment authorizations:    {payment_count} (expected 1)")
    print()

    if same_id and trip_count == 1:
        print("OK: duplicate request was deduplicated — only one trip was created.")
    else:
        print("FAIL: idempotency key did not prevent a duplicate trip.")


if __name__ == "__main__":
    main()
