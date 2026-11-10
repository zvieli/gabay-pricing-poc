"""Read declared apartment areas; verify multi-level totals without counting them twice."""
from collections import defaultdict
from pathlib import Path
import math

from openpyxl import load_workbook

from gabay_pricing.models import Apartment


def number(value):
    if value is None or value == "":
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid non-negative numeric workbook value: {value!r}")
    return result


def read_apartments(path: Path, expected_count: int | None = None, garden_overrides: dict[str, str] | None = None) -> list[Apartment]:
    workbook = load_workbook(path, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        header = [sheet.cell(1, c).value for c in range(1, 8)]
        if header != ["מס' קומה", "מספר דירה", "מס' חדרים", 'שטח דירה (מ"ר)', "שטח מרפסת", "כיווני אוויר", "הערות"]:
            raise ValueError("Unsupported workbook headers; refusing positional reinterpretation")
        grouped = defaultdict(list)
        for row in sheet.iter_rows(min_row=2, max_col=7):
            values = [c.value for c in row]
            if all(v is None for v in values):
                continue
            if not isinstance(values[1], (int, float)) or int(values[1]) != values[1]:
                raise ValueError(f"Missing/inconsistent apartment ID at row {row[0].row}")
            grouped[str(int(values[1]))].append((row[0].row, values))
        if not grouped:
            raise ValueError("Workbook has no apartment rows")
        if expected_count is not None and len(grouped) != expected_count:
            raise ValueError(f"Expected {expected_count} unique apartments, observed {len(grouped)}")
        result = []
        for apartment_id, rows in sorted(grouped.items(), key=lambda x: int(x[0])):
            levels = [(r, v) for r, v in rows if v[0] != 'סה"כ']
            summaries = [(r, v) for r, v in rows if v[0] == 'סה"כ']
            if not levels:
                raise ValueError(f"Apartment {apartment_id}: summary without levels")
            room_values = {number(v[2]) for _, v in rows if v[2] is not None}
            if len(room_values) > 1:
                raise ValueError(f"Apartment {apartment_id}: contradictory repeated room counts")
            notes = {str(v[6]) for _, v in rows if v[6]}
            kind = "triplex" if "טריפלקס" in notes else "duplex" if "דופלקס" in notes else "garden" if "דירת גן" in notes else "apartment"
            floors = [0 if v[0] == "קרקע" else int(v[0]) if isinstance(v[0], (int, float)) else None for _, v in levels]
            if len(set(floors)) != len(floors):
                raise ValueError(f"Apartment {apartment_id}: duplicate floor rows")
            if kind in {"duplex", "triplex"} and len(levels) != {"duplex": 2, "triplex": 3}[kind]:
                raise ValueError(f"Apartment {apartment_id}: incomplete multi-floor specification")
            built = [number(v[3]) for _, v in levels]
            balconies = [number(v[4]) for _, v in levels]
            area = round(sum(built), 4) if all(x is not None for x in built) else None
            balcony = round(sum(balconies), 4) if all(x is not None for x in balconies) else None
            if any(number(v[3]) is None or area is None or abs(number(v[3]) - area) > .05 for _, v in summaries):
                raise ValueError(f"Apartment {apartment_id}: floor sum disagrees with declared total")
            warnings = ["Declared apartment-area column used as built basis; not legally measured"]
            garden_area = None
            interpretation = (garden_overrides or {}).get(apartment_id, 'garden') if kind == 'garden' else 'balcony'
            if kind == 'garden':
                if interpretation == 'garden':
                    garden_area, balcony = balcony, None
                    warnings.append('Garden area INFERRED from workbook balcony column because property type is garden; verify or override interpretation')
                elif interpretation == 'unknown':
                    balcony = None
                    warnings.append('Outdoor column interpretation explicitly unresolved; source value retained in provenance')
                else:
                    warnings.append('User override: outdoor column treated as balcony; garden area remains unknown')
            for name, missing in [("rooms", not room_values), ("built_area", area is None), ("balcony_area", balcony is None and kind != 'garden'), ("floor", None in floors), ("directions", any(v[5] is None for _, v in levels))]:
                if missing:
                    warnings.append(f"Missing apartment attribute: {name}")
            addresses = ",".join(str(r) for r, _ in levels)
            result.append(Apartment(
                id=apartment_id, property_type=kind, rooms=next(iter(room_values), None), built_area=area,
                balcony_area=balcony, garden_area=garden_area, floors=[f for f in floors if f is not None],
                directions=sorted({v[5] for _, v in levels if v[5]}), source_rows=[r for r, _ in rows],
                summary_rows=[r for r, _ in summaries], floor_areas={f: a for f, a in zip(floors, built) if f is not None and a is not None},
                attribute_states={'built_area': 'OBSERVED' if area is not None else 'MISSING', 'rooms': 'OBSERVED' if room_values else 'MISSING', 'floor': 'OBSERVED' if None not in floors else 'MISSING', 'directions': 'OBSERVED' if all(v[5] is not None for _, v in levels) else 'MISSING', 'balcony_area': 'OBSERVED' if balcony is not None else 'MISSING' if kind != 'garden' else 'UNKNOWN', 'garden_area': 'INFERRED' if garden_area is not None else 'MISSING' if kind == 'garden' and interpretation == 'garden' else 'UNKNOWN' if kind == 'garden' else 'NOT_APPLICABLE'},
                provenance={"built_area": f"{sheet.title}!D[{addresses}] sum excluding summary rows", "balcony_area": f"{sheet.title}!E[{addresses}] interpretation={interpretation}", "garden_area": f"{sheet.title}!E[{addresses}] interpretation={interpretation}; source values={balconies}" if kind == 'garden' else "NOT_APPLICABLE_FOR_PROPERTY_TYPE; not a legal claim",  "rooms": f"{sheet.title}!C[{addresses}] repeated apartment total, not summed", "floors": f"{sheet.title}!A[{addresses}]", "property_type": f"{sheet.title}!G[{addresses}]", "directions": f"{sheet.title}!F[{addresses}]"}, warnings=warnings,
            ))
        return result
    finally:
        workbook.close()
