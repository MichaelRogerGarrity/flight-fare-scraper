from flight_fare_scraper.models import FlightResult


def make_result(**overrides) -> FlightResult:
    # Distinct values per column where possible, so a value written into the wrong column is caught.
    fields = dict(
        site="kayak", origin="MIA", destination="TYO",
        depart_date="2026-11-27", return_date="2026-12-13",
        price=1178.0, currency="USD", booking_provider="Expedia",
        outbound_airline="Air Canada", outbound_operated_by="Air Canada Express - Jazz",
        outbound_depart="2026-11-27T09:55:00", outbound_arrive="2026-11-28T16:30:00",
        outbound_stops=1, outbound_duration_min=995,
        outbound_layover_airports="YUL", outbound_layover_min=62, outbound_airport_change=False,
        outbound_equipment="CRJ-900",
        return_airline="ANA", return_operated_by="United Airlines",
        return_depart="2026-12-13T18:45:00", return_arrive="2026-12-13T20:10:00",
        return_stops=2, return_duration_min=925,
        return_layover_airports="ORD,YYZ", return_layover_min=95, return_airport_change=True,
        return_equipment="Boeing 787-9",
        cabin_class="Economy", carry_on_status="FEE", carry_on_fee=25.0,
        checked_bag_status="INCLUDED", checked_bag_fee=110.0, second_checked_bag_fee=130.0,
        free_cancellation=True, virtual_interline=True, self_transfer_protection=False,
        price_prediction="WAIT", price_prediction_change=-12.0, price_prediction_days=15,
        booking_url="https://www.kayak.com/book/flight?code=test",
    )
    fields.update(overrides)
    return FlightResult(**fields)
