import logging

from decimal import Decimal, InvalidOperation
from psycopg.rows import dict_row

from cost_database import cost_pool
from services.cost_schema_service import ensure_cost_settings_schema
from shared.db import get_export_connection


PROFILE_FIELDS = (
    "currency", "distribution_default_percent", "cleaning_cost_per_minute",
    "reception_cost_per_hour", "room_rent_percent", "breakfast_calculation_basis",
    "breakfast_food_cost_per_guest", "breakfast_staff_cost_per_hour",
    "breakfast_rent_percent", "parking_rent_percent", "card_cost_percent",
)

DEFAULT_PROFILE = {
    "currency": "SEK", "distributionDefaultPercent": "0",
    "cleaningCostPerMinute": "0", "receptionCostPerHour": "0",
    "roomRentPercent": "0", "breakfastCalculationBasis": "guests", "breakfastFoodCostPerGuest": "0",
    "breakfastStaffCostPerHour": "0", "breakfastRentPercent": "0",
    "parkingRentPercent": "0", "cardCostPercent": "2",
}

SOURCE_PROPERTIES_SQL = """
    SELECT id::text AS enterprise_id, trim(name)::text AS hotel_name
    FROM enterprise_current
    WHERE tenant_key = 'GCCH'
      AND name IS NOT NULL
      AND trim(name) <> ''
    ORDER BY hotel_name, enterprise_id
"""

IMPORTED_PROPERTIES_SQL = """
    SELECT DISTINCT ON (enterprise_id)
        enterprise_id,
        hotel_name
    FROM (
        SELECT enterprise_id::text, trim(hotel_name)::text AS hotel_name, -1 AS priority
        FROM functions.hotels
        WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

        UNION ALL

        SELECT enterprise_id::text, trim(hotel_name)::text AS hotel_name, 1 AS priority
        FROM functions.breakfast_data
        WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

        UNION ALL

        SELECT enterprise_id::text, trim(hotel_name)::text, 2
        FROM functions.parking_data
        WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

        UNION ALL

        SELECT enterprise_id::text, trim(hotel_name)::text, 3
        FROM functions.total_payment_data
        WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

        UNION ALL

        SELECT enterprise_id::text, trim(hotel_name)::text, 4
        FROM functions.room_revenue_night_data
        WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL
    ) imported
    ORDER BY enterprise_id, priority, hotel_name
"""


def _camel(name):
    parts = name.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _json_row(row):
    return {_camel(key): str(value) if isinstance(value, Decimal) else value for key, value in row.items()}


def _property_json(row):
    return {
        "enterpriseId": str(row["enterprise_id"]),
        "hotelName": row["hotel_name"],
    }


def _list_source_properties():
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SOURCE_PROPERTIES_SQL)
            return [_property_json(row) for row in cursor.fetchall()]


def _list_imported_properties():
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(IMPORTED_PROPERTIES_SQL)
            return [_property_json(row) for row in cursor.fetchall()]


def _list_mirrored_properties():
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT enterprise_id, hotel_name
                FROM functions.hotels
                WHERE tenant_key = 'GCCH' AND active
                ORDER BY hotel_name, enterprise_id
            """)
            return [_property_json(row) for row in cursor.fetchall()]


def _get_mirrored_property(enterprise_id):
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT enterprise_id, hotel_name
                FROM functions.hotels
                WHERE enterprise_id = %s
                """,
                (enterprise_id,),
            )
            row = cursor.fetchone()
    return _property_json(row) if row is not None else None


def _upsert_mirrored_properties(properties):
    if not properties:
        return
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO functions.hotels (
                    enterprise_id, tenant_key, hotel_name
                )
                VALUES (%s, 'GCCH', %s)
                ON CONFLICT (enterprise_id) DO UPDATE SET
                    tenant_key = EXCLUDED.tenant_key,
                    hotel_name = EXCLUDED.hotel_name,
                    active = true,
                    last_seen_at = now(),
                    last_updated_at = CASE
                        WHEN functions.hotels.hotel_name
                            IS DISTINCT FROM EXCLUDED.hotel_name
                        THEN now()
                        ELSE functions.hotels.last_updated_at
                    END
                """,
                [
                    (property_row["enterpriseId"], property_row["hotelName"])
                    for property_row in properties
                ],
            )


def _get_imported_property(enterprise_id):
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                SELECT enterprise_id, hotel_name
                FROM ({IMPORTED_PROPERTIES_SQL.rstrip().rstrip(';')}) imported_properties
                WHERE enterprise_id = %s
                LIMIT 1
                """,
                (enterprise_id,),
            )
            row = cursor.fetchone()
    return _property_json(row) if row is not None else None


def _get_preloaded_property(enterprise_id):
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT settings.enterprise_id, hotel.hotel_name
                FROM functions.cost_property_settings settings
                JOIN functions.hotels hotel USING (enterprise_id)
                WHERE settings.enterprise_id = %s
                """,
                (enterprise_id,),
            )
            row = cursor.fetchone()
    return _property_json(row) if row is not None else None


def _preload_property_settings(properties):
    if not properties:
        return

    ensure_cost_settings_schema()
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO functions.cost_property_settings (enterprise_id)
                VALUES (%s)
                ON CONFLICT (enterprise_id) DO NOTHING
                """,
                [
                    (property_row["enterpriseId"],)
                    for property_row in properties
                ],
            )


def list_cost_settings_hotels():
    # Normal page reads stay entirely inside Database A. Database B is only used
    # to bootstrap an empty mirror; the scheduled properties pipeline keeps the
    # mirror authoritative afterwards.
    ensure_cost_settings_schema()
    properties = _list_mirrored_properties()
    if properties:
        _preload_property_settings(properties)
        return properties

    try:
        properties = _list_source_properties()
        if properties:
            _upsert_mirrored_properties(properties)
            _preload_property_settings(properties)
            return properties
        logging.warning(
            "enterprise_current returned no GCCH properties; using imported cost facts"
        )
    except Exception:
        logging.warning(
            "Unable to read enterprise_current; using imported cost facts",
            exc_info=True,
        )

    properties = sorted(
        _list_imported_properties(),
        key=lambda property_row: (
            property_row["hotelName"].casefold(),
            property_row["enterpriseId"],
        ),
    )
    _preload_property_settings(properties)
    return properties


def _get_cost_settings_hotel(enterprise_id):
    enterprise_id = _required_text(enterprise_id, "Enterprise ID", 250)

    mirrored_property = _get_mirrored_property(enterprise_id)
    if mirrored_property is not None:
        return mirrored_property

    # Bootstrap compatibility for a mirror that has not yet been populated by
    # the timer. This is a two-connection application transfer, not a cross-DB SQL join.
    try:
        with get_export_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id::text AS enterprise_id, trim(name)::text AS hotel_name
                    FROM enterprise_current
                    WHERE tenant_key = 'GCCH' AND id::text = %s
                    """,
                    (enterprise_id,),
                )
                row = cursor.fetchone()
        if row is not None:
            property_record = _property_json(row)
            _upsert_mirrored_properties([property_record])
            return property_record
    except Exception:
        logging.warning(
            "Unable to resolve property from enterprise_current enterprise_id=%s",
            enterprise_id,
            exc_info=True,
        )

    property_record = _get_imported_property(enterprise_id)
    if property_record is None:
        raise ValueError(
            "Property was not found in synchronized cost properties. "
            "Run the properties cost-data import."
        )
    return property_record


def _resolve_cost_settings_hotel(enterprise_id, fallback_hotel_name=None):
    preloaded_property = _get_preloaded_property(enterprise_id)
    if preloaded_property is not None:
        return preloaded_property

    try:
        return _get_cost_settings_hotel(enterprise_id)
    except ValueError:
        if not fallback_hotel_name:
            raise
        logging.warning(
            "Using supplied property name after lookup miss enterprise_id=%s",
            enterprise_id,
        )
        return {
            "enterpriseId": _required_text(
                enterprise_id,
                "Enterprise ID",
                250,
            ),
            "hotelName": _required_text(
                fallback_hotel_name,
                "Hotel",
                250,
            ),
        }


def fetch_cost_settings(enterprise_id, hotel_name=None):
    ensure_cost_settings_schema()
    if hotel_name is None:
        property_record = _resolve_cost_settings_hotel(enterprise_id)
        enterprise_id = property_record["enterpriseId"]
        hotel_name = property_record["hotelName"]
    else:
        # The ID/name pair came from the property-list endpoint. Persist it
        # locally on first load as well, so a cached list response cannot leave
        # the subsequent Save request without a Database A property record.
        property_record = {
            "enterpriseId": _required_text(
                enterprise_id,
                "Enterprise ID",
                250,
            ),
            "hotelName": _required_text(hotel_name, "Hotel", 250),
        }
        enterprise_id = property_record["enterpriseId"]
        hotel_name = property_record["hotelName"]
        _upsert_mirrored_properties([property_record])
        _preload_property_settings([property_record])
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT settings.*, hotel.hotel_name
                FROM functions.cost_property_settings settings
                JOIN functions.hotels hotel USING (enterprise_id)
                WHERE settings.enterprise_id = %s
            """, (enterprise_id,))
            profile_row = cursor.fetchone()
            profile = dict(DEFAULT_PROFILE)
            if profile_row:
                profile.update(_json_row(profile_row))

            cursor.execute("""
                SELECT g.distribution_group_id, g.group_name, g.cost_percent,
                    coalesce(json_agg(json_build_object('matchType', r.match_type, 'matchValue', r.match_value)
                        ORDER BY r.distribution_rule_id) FILTER (WHERE r.distribution_rule_id IS NOT NULL), '[]') AS rules
                FROM functions.cost_distribution_groups g
                LEFT JOIN functions.cost_distribution_rules r USING (distribution_group_id)
                WHERE g.enterprise_id = %s GROUP BY g.distribution_group_id ORDER BY g.sort_order, g.distribution_group_id
            """, (enterprise_id,))
            distribution = [_json_row(row) for row in cursor.fetchall()]

            collections = {}
            for name, query in {
                "cleaningCategories": "SELECT category_name, min_guests, max_guests, cleaning_minutes, linen_cost FROM functions.cost_cleaning_categories WHERE enterprise_id = %s ORDER BY sort_order, cleaning_category_id",
                "arrivalTiers": "SELECT min_arrivals, max_arrivals, reception_hours FROM functions.cost_arrival_staffing_tiers WHERE enterprise_id = %s ORDER BY sort_order, arrival_tier_id",
                "breakfastTiers": "SELECT min_guests, max_guests, staff_hours FROM functions.cost_breakfast_staffing_tiers WHERE enterprise_id = %s ORDER BY sort_order, breakfast_tier_id",
                "fixedCosts": "SELECT cost_name, amount, cadence, active FROM functions.cost_fixed_lines WHERE enterprise_id = %s ORDER BY sort_order, fixed_cost_line_id",
            }.items():
                cursor.execute(query, (enterprise_id,))
                collections[name] = [_json_row(row) for row in cursor.fetchall()]

    profile.pop("hotelName", None)
    profile.pop("enterpriseId", None)
    profile.pop("updatedAt", None)
    resolved_name = profile_row["hotel_name"] if profile_row else hotel_name
    return {"enterpriseId": enterprise_id, "hotelName": resolved_name, "profile": profile, "distributionGroups": distribution, **collections}


def _number(value, label, maximum=None, integer=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if result < 0 or (maximum is not None and result > maximum):
        suffix = f" between 0 and {maximum}" if maximum is not None else " zero or greater"
        raise ValueError(f"{label} must be{suffix}")
    if integer and result != result.to_integral_value():
        raise ValueError(f"{label} must be a whole number")
    return int(result) if integer else result


def _required_text(value, label, max_length=200):
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label} is required")
    if len(result) > max_length:
        raise ValueError(f"{label} is too long")
    return result


def _validate_ranges(rows, min_key, max_key, label):
    ranges = []
    for index, row in enumerate(rows):
        minimum = _number(row.get(min_key), f"{label} row {index + 1} minimum", integer=True)
        maximum = row.get(max_key)
        maximum = None if maximum in (None, "") else _number(maximum, f"{label} row {index + 1} maximum", integer=True)
        if maximum is not None and maximum < minimum:
            raise ValueError(f"{label} row {index + 1} maximum cannot be below its minimum")
        ranges.append((minimum, maximum))
    ordered = sorted(ranges)
    for previous, current in zip(ordered, ordered[1:]):
        if previous[1] is None or current[0] <= previous[1]:
            raise ValueError(f"{label} ranges cannot overlap")
    return ranges


def validate_cost_settings(enterprise_id, hotel_name, payload):
    enterprise_id = _required_text(enterprise_id, "Enterprise ID", 50)
    hotel_name = _required_text(hotel_name, "Hotel", 250)
    profile = payload.get("profile") or {}
    currency = _required_text(profile.get("currency", "SEK"), "Currency", 3).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code")
    percent_fields = {"distributionDefaultPercent", "roomRentPercent", "breakfastRentPercent", "parkingRentPercent", "cardCostPercent"}
    clean_profile = {"currency": currency}
    for database_field in PROFILE_FIELDS[1:]:
        key = _camel(database_field)
        if key == "breakfastCalculationBasis":
            basis = profile.get(key, "guests")
            if basis not in {"guests", "products"}:
                raise ValueError("Breakfast calculation basis must be guests or products")
            clean_profile[key] = basis
            continue
        clean_profile[key] = _number(profile.get(key, DEFAULT_PROFILE[key]), key, 100 if key in percent_fields else None)

    result = {"enterpriseId": enterprise_id, "hotelName": hotel_name, "profile": clean_profile}
    groups = payload.get("distributionGroups") or []
    names = set()
    result["distributionGroups"] = []
    for index, group in enumerate(groups):
        name = _required_text(group.get("groupName"), f"Distribution group {index + 1} name")
        if name.casefold() in names: raise ValueError("Distribution group names must be unique")
        names.add(name.casefold())
        rules = []
        for rule in group.get("rules") or []:
            match_type = rule.get("matchType")
            if match_type not in {"rate", "channel"}: raise ValueError("Distribution match type must be rate or channel")
            rules.append({"matchType": match_type, "matchValue": _required_text(rule.get("matchValue"), "Distribution match value")})
        result["distributionGroups"].append({"groupName": name, "costPercent": _number(group.get("costPercent"), f"{name} percent", 100), "rules": rules})

    cleaning = payload.get("cleaningCategories") or []
    _validate_ranges(cleaning, "minGuests", "maxGuests", "Cleaning")
    result["cleaningCategories"] = [{
        "categoryName": _required_text(row.get("categoryName"), "Cleaning category name"),
        "minGuests": _number(row.get("minGuests"), "Minimum guests", integer=True),
        "maxGuests": None if row.get("maxGuests") in (None, "") else _number(row.get("maxGuests"), "Maximum guests", integer=True),
        "cleaningMinutes": _number(row.get("cleaningMinutes"), "Cleaning minutes"),
        "linenCost": _number(row.get("linenCost"), "Linen cost"),
    } for row in cleaning]

    for source, target, min_key, max_key, hours_key, label in (
        (payload.get("arrivalTiers") or [], "arrivalTiers", "minArrivals", "maxArrivals", "receptionHours", "Arrival staffing"),
        (payload.get("breakfastTiers") or [], "breakfastTiers", "minGuests", "maxGuests", "staffHours", "Breakfast staffing"),
    ):
        _validate_ranges(source, min_key, max_key, label)
        result[target] = [{min_key: _number(row.get(min_key), f"{label} minimum", integer=True), max_key: None if row.get(max_key) in (None, "") else _number(row.get(max_key), f"{label} maximum", integer=True), hours_key: _number(row.get(hours_key), f"{label} hours")} for row in source]

    result["fixedCosts"] = [{"costName": _required_text(row.get("costName"), "Fixed cost name"), "amount": _number(row.get("amount"), "Fixed cost amount"), "cadence": row.get("cadence", "monthly"), "active": bool(row.get("active", True))} for row in payload.get("fixedCosts") or []]
    if any(row["cadence"] not in {"daily", "monthly", "yearly"} for row in result["fixedCosts"]): raise ValueError("Fixed cost cadence must be daily, monthly, or yearly")
    for collection, field, label in ((result["cleaningCategories"], "categoryName", "Cleaning category"), (result["fixedCosts"], "costName", "Fixed cost")):
        values = [row[field].casefold() for row in collection]
        if len(values) != len(set(values)): raise ValueError(f"{label} names must be unique")
    return result


def save_cost_settings(enterprise_id, payload):
    ensure_cost_settings_schema()
    # The property picker already obtained this ID/name pair from the property
    # list endpoint. The submitted name is a fallback if a repeated source
    # lookup is temporarily unavailable.
    property_record = _resolve_cost_settings_hotel(
        enterprise_id,
        payload.get("hotelName"),
    )
    data = validate_cost_settings(
        property_record["enterpriseId"],
        property_record["hotelName"],
        payload,
    )
    p = data["profile"]
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO functions.cost_property_settings (enterprise_id, currency, distribution_default_percent, cleaning_cost_per_minute, reception_cost_per_hour, room_rent_percent, breakfast_calculation_basis, breakfast_food_cost_per_guest, breakfast_staff_cost_per_hour, breakfast_rent_percent, parking_rent_percent, card_cost_percent) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (enterprise_id) DO UPDATE SET currency=EXCLUDED.currency, distribution_default_percent=EXCLUDED.distribution_default_percent, cleaning_cost_per_minute=EXCLUDED.cleaning_cost_per_minute, reception_cost_per_hour=EXCLUDED.reception_cost_per_hour, room_rent_percent=EXCLUDED.room_rent_percent, breakfast_calculation_basis=EXCLUDED.breakfast_calculation_basis, breakfast_food_cost_per_guest=EXCLUDED.breakfast_food_cost_per_guest, breakfast_staff_cost_per_hour=EXCLUDED.breakfast_staff_cost_per_hour, breakfast_rent_percent=EXCLUDED.breakfast_rent_percent, parking_rent_percent=EXCLUDED.parking_rent_percent, card_cost_percent=EXCLUDED.card_cost_percent, updated_at=now()""", (data["enterpriseId"], p["currency"], p["distributionDefaultPercent"], p["cleaningCostPerMinute"], p["receptionCostPerHour"], p["roomRentPercent"], p["breakfastCalculationBasis"], p["breakfastFoodCostPerGuest"], p["breakfastStaffCostPerHour"], p["breakfastRentPercent"], p["parkingRentPercent"], p["cardCostPercent"]))
            for table in ("cost_distribution_groups", "cost_cleaning_categories", "cost_arrival_staffing_tiers", "cost_breakfast_staffing_tiers", "cost_fixed_lines"):
                cursor.execute(f"DELETE FROM functions.{table} WHERE enterprise_id = %s", (data["enterpriseId"],))
            for order, group in enumerate(data["distributionGroups"]):
                cursor.execute("INSERT INTO functions.cost_distribution_groups (enterprise_id, group_name, cost_percent, sort_order) VALUES (%s,%s,%s,%s) RETURNING distribution_group_id", (data["enterpriseId"], group["groupName"], group["costPercent"], order)); group_id = cursor.fetchone()[0]
                cursor.executemany("INSERT INTO functions.cost_distribution_rules (distribution_group_id, match_type, match_value) VALUES (%s,%s,%s)", [(group_id, r["matchType"], r["matchValue"]) for r in group["rules"]])
            cursor.executemany("INSERT INTO functions.cost_cleaning_categories (enterprise_id, category_name, min_guests, max_guests, cleaning_minutes, linen_cost, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s)", [(data["enterpriseId"], r["categoryName"], r["minGuests"], r["maxGuests"], r["cleaningMinutes"], r["linenCost"], i) for i,r in enumerate(data["cleaningCategories"])])
            cursor.executemany("INSERT INTO functions.cost_arrival_staffing_tiers (enterprise_id, min_arrivals, max_arrivals, reception_hours, sort_order) VALUES (%s,%s,%s,%s,%s)", [(data["enterpriseId"], r["minArrivals"], r["maxArrivals"], r["receptionHours"], i) for i,r in enumerate(data["arrivalTiers"])])
            cursor.executemany("INSERT INTO functions.cost_breakfast_staffing_tiers (enterprise_id, min_guests, max_guests, staff_hours, sort_order) VALUES (%s,%s,%s,%s,%s)", [(data["enterpriseId"], r["minGuests"], r["maxGuests"], r["staffHours"], i) for i,r in enumerate(data["breakfastTiers"])])
            cursor.executemany("INSERT INTO functions.cost_fixed_lines (enterprise_id, cost_name, amount, cadence, active, sort_order) VALUES (%s,%s,%s,%s,%s,%s)", [(data["enterpriseId"], r["costName"], r["amount"], r["cadence"], r["active"], i) for i,r in enumerate(data["fixedCosts"])])
    return fetch_cost_settings(data["enterpriseId"], data["hotelName"])
