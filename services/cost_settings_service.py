from decimal import Decimal, InvalidOperation
from psycopg.rows import dict_row

from cost_database import cost_pool
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


def _camel(name):
    parts = name.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _json_row(row):
    return {_camel(key): str(value) if isinstance(value, Decimal) else value for key, value in row.items()}


def list_cost_settings_hotels():
    sql = """
        SELECT id::text AS enterprise_id, trim(name)::text AS hotel_name
        FROM enterprise_current
        WHERE tenant_key = 'GCCH'
          AND name IS NOT NULL
          AND trim(name) <> ''
        ORDER BY hotel_name, enterprise_id
    """
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return [
                {"enterpriseId": str(row["enterprise_id"]), "hotelName": row["hotel_name"]}
                for row in cursor.fetchall()
            ]


def _get_cost_settings_hotel(enterprise_id):
    enterprise_id = _required_text(enterprise_id, "Enterprise ID", 250)

    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text AS enterprise_id, trim(name)::text AS hotel_name
                FROM enterprise_current
                WHERE tenant_key = 'GCCH' AND id = %s
                """,
                (enterprise_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise ValueError("Property was not found in enterprise_current")
    return {"enterpriseId": str(row["enterprise_id"]), "hotelName": row["hotel_name"]}


def fetch_cost_settings(enterprise_id, hotel_name=None):
    if hotel_name is None:
        property_record = _get_cost_settings_hotel(enterprise_id)
        enterprise_id = property_record["enterpriseId"]
        hotel_name = property_record["hotelName"]
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM functions.cost_property_settings WHERE enterprise_id = %s", (enterprise_id,))
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
    property_record = _get_cost_settings_hotel(enterprise_id)
    data = validate_cost_settings(
        property_record["enterpriseId"],
        property_record["hotelName"],
        payload,
    )
    p = data["profile"]
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO functions.cost_property_settings (enterprise_id, hotel_name, currency, distribution_default_percent, cleaning_cost_per_minute, reception_cost_per_hour, room_rent_percent, breakfast_calculation_basis, breakfast_food_cost_per_guest, breakfast_staff_cost_per_hour, breakfast_rent_percent, parking_rent_percent, card_cost_percent) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (enterprise_id) DO UPDATE SET hotel_name=EXCLUDED.hotel_name, currency=EXCLUDED.currency, distribution_default_percent=EXCLUDED.distribution_default_percent, cleaning_cost_per_minute=EXCLUDED.cleaning_cost_per_minute, reception_cost_per_hour=EXCLUDED.reception_cost_per_hour, room_rent_percent=EXCLUDED.room_rent_percent, breakfast_calculation_basis=EXCLUDED.breakfast_calculation_basis, breakfast_food_cost_per_guest=EXCLUDED.breakfast_food_cost_per_guest, breakfast_staff_cost_per_hour=EXCLUDED.breakfast_staff_cost_per_hour, breakfast_rent_percent=EXCLUDED.breakfast_rent_percent, parking_rent_percent=EXCLUDED.parking_rent_percent, card_cost_percent=EXCLUDED.card_cost_percent, updated_at=now()""", (data["enterpriseId"], data["hotelName"], p["currency"], p["distributionDefaultPercent"], p["cleaningCostPerMinute"], p["receptionCostPerHour"], p["roomRentPercent"], p["breakfastCalculationBasis"], p["breakfastFoodCostPerGuest"], p["breakfastStaffCostPerHour"], p["breakfastRentPercent"], p["parkingRentPercent"], p["cardCostPercent"]))
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
