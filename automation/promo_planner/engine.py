from __future__ import annotations
import math
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from .store import Campaign

PRICING_SHEET = "Pricing"
PROMOS_SHEET = "Promos"
PROMO_NAME_COLUMN = "PromoName"
ORIGINAL_PRICE_EUR_COLUMN = "_PromoPlannerOriginalPrice_EUR"
ORIGINAL_PRICE_USD_COLUMN = "_PromoPlannerOriginalPrice_USD"
PRICE_OWNER_COLUMN = "_PromoPlannerPriceOwner"


@dataclass
class PromoOption:
    code: str
    promo_type: str
    value: float
    label: str

    @property
    def display(self) -> str:
        suffix = "%" if self.promo_type == "percent" else ""
        return f"{self.label}  [{self.code}]  · {self.value:g}{suffix}"


@dataclass
class Catalogue:
    destinations: list[tuple[str, str]]
    plans: list[str]
    days: list[int]
    promos: list[PromoOption]


@dataclass
class Preview:
    action: str
    matched_rows: int
    changed_rows: int
    below_floor_rows: int
    collisions: list[str]
    examples: list[str]


def _text(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s


def _num(v):
    try:
        if v is None or (isinstance(v, str) and v.startswith("=")):
            return None
        return float(v)
    except Exception:
        return None


def round_promo_price(base: float, ptype: str, value: float) -> float:
    if ptype == "percent":
        raw = base * (1.0 - value / 100.0)
    elif ptype == "absolute":
        raw = base - value
    else:
        raw = base
    return math.floor(max(raw, 0.0) * 20.0 + 1e-12) / 20.0


def _headers(ws) -> dict[str, int]:
    return {_text(cell.value): cell.column for cell in ws[1] if _text(cell.value)}


def _find_col(h: dict[str, int], *names: str) -> int | None:
    lower = {k.lower(): v for k, v in h.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _read_promos(wb) -> list[PromoOption]:
    if PROMOS_SHEET not in wb.sheetnames:
        return []
    ws = wb[PROMOS_SHEET]
    h = _headers(ws)
    c_code = _find_col(h, "PromoCode", "Code", "_PromoCode")
    c_type = _find_col(h, "PromoType", "Type", "_Type")
    c_value = _find_col(h, "PromoValue", "Value", "RawValue", "_RawValue")
    c_label = _find_col(h, "PromoLabel", "PromoName", "Name", "Label")
    if not (c_code and c_type and c_value):
        return []
    out=[]
    for r in range(2, ws.max_row+1):
        code=_text(ws.cell(r,c_code).value)
        ptype=_text(ws.cell(r,c_type).value).lower()
        value=_num(ws.cell(r,c_value).value)
        label=_text(ws.cell(r,c_label).value) if c_label else code
        if not code or value is None:
            continue
        if ptype in {"percentage","pct","%"}: ptype="percent"
        if ptype in {"fixed","amount","value"}: ptype="absolute"
        if ptype not in {"percent","absolute"}: continue
        out.append(PromoOption(code,ptype,float(value),label or code))
    return sorted(out,key=lambda x:(x.label.lower(),x.code.lower()))


def read_catalogue(path: Path) -> Catalogue:
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        ws = wb[PRICING_SHEET] if PRICING_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        h = _headers(ws)
        required = {"Country", "ISO", "Plan", "Days"}
        missing = required - set(h)
        if missing:
            raise ValueError("Pricing workbook is missing columns: " + ", ".join(sorted(missing)))
        dest: dict[str, str] = {}
        plans = set(); days = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            code = _text(row[h["ISO"] - 1]).upper()
            country = _text(row[h["Country"] - 1])
            plan = _text(row[h["Plan"] - 1])
            day = _num(row[h["Days"] - 1])
            if code: dest[code] = country or code
            if plan: plans.add(plan)
            if day is not None and day > 0: days.add(int(day))
        return Catalogue(sorted(dest.items(), key=lambda x: (x[1].lower(), x[0])), sorted(plans), sorted(days), _read_promos(wb))
    finally:
        wb.close()


def _matches(row, h: dict[str, int], c: Campaign) -> bool:
    iso = _text(row[h["ISO"] - 1]).upper()
    plan = _text(row[h["Plan"] - 1])
    day = _num(row[h["Days"] - 1])
    return iso in set(c.destinations) and plan in set(c.plans) and day is not None and int(day) in set(c.days)


def preview_campaign(path: Path, c: Campaign, action: str) -> Preview:
    action = action.upper()
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        ws = wb[PRICING_SHEET] if PRICING_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        h = _headers(ws)
        for col in ("ISO", "Plan", "Days", "PromoCode"):
            if col not in h: raise ValueError(f"Pricing workbook is missing {col}.")
        matched = changed = below = 0; collisions=[]; examples=[]
        for values in ws.iter_rows(min_row=2, values_only=True):
            if not _matches(values, h, c): continue
            matched += 1
            current = _text(values[h["PromoCode"] - 1])
            iso = _text(values[h["ISO"] - 1]); plan = _text(values[h["Plan"] - 1]); day = int(_num(values[h["Days"] - 1]) or 0)
            if action == "START":
                if current and current != c.promo_code:
                    collisions.append(f"{iso} / {plan} / {day}d already has {current}"); continue
                if current != c.promo_code: changed += 1
                for currency in ("EUR", "USD"):
                    pcol, fcol = f"Price_{currency}", f"CostFloor_{currency}"
                    if pcol in h and fcol in h:
                        base = _num(values[h[pcol]-1]); floor = _num(values[h[fcol]-1])
                        if base is not None and floor is not None and round_promo_price(base,c.promo_type,c.promo_value) < floor - 1e-9:
                            below += 1; break
            elif action == "END" and current == c.promo_code:
                changed += 1
            if len(examples) < 8: examples.append(f"{iso} · {plan} · {day}d · current promo: {current or 'none'}")
        return Preview(action, matched, changed, below, collisions[:100], examples)
    finally:
        wb.close()


def _ensure_promo_name_column(ws, h: dict[str,int]) -> dict[str,int]:
    if PROMO_NAME_COLUMN not in h:
        col = ws.max_column + 1
        ws.cell(1,col).value = PROMO_NAME_COLUMN
        h[PROMO_NAME_COLUMN]=col
    return h


def _ensure_planner_price_columns(ws, h: dict[str, int]) -> dict[str, int]:
    """Ensure hidden columns used to restore temporary list-price uplifts."""
    for name in (ORIGINAL_PRICE_EUR_COLUMN, ORIGINAL_PRICE_USD_COLUMN, PRICE_OWNER_COLUMN):
        if name not in h:
            col = ws.max_column + 1
            ws.cell(1, col).value = name
            h[name] = col
        try:
            ws.column_dimensions[ws.cell(1, h[name]).column_letter].hidden = True
        except Exception:
            pass
    return h


def execute_campaign(path: Path, c: Campaign, action: str, backup_dir: Path) -> tuple[Path, int]:
    action = action.upper()
    preview = preview_campaign(path,c,action)
    if action == "START" and preview.collisions:
        raise RuntimeError("Promo collision detected. Resolve before execution:\n" + "\n".join(preview.collisions[:20]))
    if action == "START" and preview.below_floor_rows and not c.allow_below_cost:
        raise RuntimeError(f"{preview.below_floor_rows} rows would be below cost floor. Enable 'Allow below cost' explicitly or change the promotion.")
    backup_dir.mkdir(parents=True,exist_ok=True)
    ts=datetime.now().strftime("%Y%m%d-%H%M%S")
    backup=backup_dir / f"{path.stem}_before_{c.promo_code}_{action}_{ts}{path.suffix}"
    shutil.copy2(path,backup)
    wb=load_workbook(path,keep_vba=path.suffix.lower()==".xlsm")
    try:
        ws=wb[PRICING_SHEET] if PRICING_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        h=_ensure_promo_name_column(ws,_headers(ws))
        changed=0
        for r in range(2,ws.max_row+1):
            vals=[ws.cell(r,col).value for col in range(1,ws.max_column+1)]
            if not _matches(vals,h,c): continue
            promo_cell=ws.cell(r,h["PromoCode"]); name_cell=ws.cell(r,h[PROMO_NAME_COLUMN]); current=_text(promo_cell.value)
            if action=="START":
                if current==c.promo_code:
                    if _text(name_cell.value)!=c.promo_name: name_cell.value=c.promo_name
                    continue
                promo_cell.value=c.promo_code; name_cell.value=c.promo_name
                if c.allow_below_cost and "AllowBelowCost" in h: ws.cell(r,h["AllowBelowCost"]).value=True
                changed += 1
            elif action=="END" and current==c.promo_code:
                promo_cell.value=""; name_cell.value=""; changed += 1
        try:
            wb.calculation.fullCalcOnLoad=True; wb.calculation.forceFullCalc=True; wb.calculation.calcMode="auto"
        except Exception: pass
        wb.save(path)
    except Exception:
        try: wb.close()
        except Exception: pass
        shutil.copy2(backup,path); raise
    finally:
        try: wb.close()
        except Exception: pass
    return backup,changed


@dataclass
class BatchPreviewItem:
    campaign_id: int
    campaign_name: str
    action: str
    matched_rows: int
    changed_rows: int
    below_floor_rows: int
    collisions: list[str]


@dataclass
class CostFloorIssue:
    campaign_id: int
    campaign_name: str
    iso: str
    plan: str
    days: int
    currency: str
    current_list_price: float
    promo_net_price: float
    cost_floor: float
    target_net_price: float
    required_list_price: float


@dataclass
class PriceChange:
    iso: str
    country: str
    plan: str
    days: int
    change: str
    promo_before: str
    promo_after: str
    list_eur_before: float | None
    list_eur_after: float | None
    net_eur_before: float | None
    net_eur_after: float | None
    list_usd_before: float | None
    list_usd_after: float | None
    net_usd_before: float | None
    net_usd_after: float | None


@dataclass
class BatchPreview:
    execution_date: str
    items: list[BatchPreviewItem]
    cost_floor_issues: list[CostFloorIssue]
    price_changes: list[PriceChange]

    @property
    def total_changed_rows(self) -> int:
        return sum(i.changed_rows for i in self.items)

    @property
    def collisions(self) -> list[str]:
        out: list[str] = []
        for i in self.items:
            out.extend(i.collisions)
        return out

    @property
    def below_floor_rows(self) -> int:
        return sum(i.below_floor_rows for i in self.items)


def _ceil_price_grid(value: float) -> float:
    return math.ceil((float(value) - 1e-12) * 20.0) / 20.0


def required_list_price_for_target(target_net: float, ptype: str, value: float) -> float:
    """Return the smallest 0.05-grid list price whose promo net is >= target_net."""
    if ptype == "percent":
        factor = 1.0 - float(value) / 100.0
        if factor <= 0:
            raise ValueError("A 100% (or greater) discount cannot be lifted above the cost floor by changing list price.")
        raw = float(target_net) / factor
    elif ptype == "absolute":
        raw = float(target_net) + float(value)
    else:
        raw = float(target_net)
    candidate = _ceil_price_grid(raw)
    # Promo calculation rounds DOWN to the 0.05 grid, so verify and nudge if needed.
    while round_promo_price(candidate, ptype, value) < float(target_net) - 1e-9:
        candidate = round(candidate + 0.05, 10)
    return round(candidate, 2)


def _ordered_actions(actions: list[tuple[Campaign, str]]) -> list[tuple[Campaign, str]]:
    # A promo ending and another starting on the same pricing rows is a normal hand-over.
    # Therefore removals always happen before publications for a date batch.
    return sorted(actions, key=lambda x: 0 if x[1].upper() == "END" else 1)


def preview_batch(
    path: Path,
    actions: list[tuple[Campaign, str]],
    execution_date: str = "",
    *,
    adjust_below_floor_to_margin: bool = False,
) -> BatchPreview:
    """Simulate a whole execution batch without writing the workbook.

    The simulation is sequential (END before START), so hand-overs on the same
    pricing row are represented as one final before/after price change.
    """
    ordered = _ordered_actions(actions)
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        ws = wb[PRICING_SHEET] if PRICING_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        h = _headers(ws)
        for col in ("ISO", "Plan", "Days", "PromoCode"):
            if col not in h:
                raise ValueError(f"Pricing workbook is missing {col}.")

        rows = list(ws.iter_rows(min_row=2, values_only=True))
        promo_catalogue = {p.code: p for p in _read_promos(wb)}

        def row_value(values, name):
            return values[h[name] - 1] if name in h else None

        def promo_meta(values, code: str):
            code = _text(code)
            opt = promo_catalogue.get(code)
            if opt is not None:
                return opt.promo_type, float(opt.value), opt.label or code
            # Legacy/static fallback when an existing code is not in the hidden catalogue.
            ptype = _text(row_value(values, "PromoType")).lower()
            pvalue = _num(row_value(values, "PromoValue")) or 0.0
            plabel = _text(row_value(values, "PromoName")) or _text(row_value(values, "PromoLabel")) or code
            if ptype in {"percentage", "pct", "%"}:
                ptype = "percent"
            if ptype in {"fixed", "amount", "value"}:
                ptype = "absolute"
            return ptype, float(pvalue), plabel or code

        states = []
        for values in rows:
            code = _text(row_value(values, "PromoCode"))
            ptype, pvalue, plabel = promo_meta(values, code) if code else ("", 0.0, "")
            eur = _num(row_value(values, "Price_EUR"))
            usd = _num(row_value(values, "Price_USD"))
            states.append({
                "promo_code": code,
                "promo_type": ptype,
                "promo_value": pvalue,
                "promo_label": plabel,
                "list_eur": eur,
                "list_usd": usd,
                "original_list_eur": _num(row_value(values, ORIGINAL_PRICE_EUR_COLUMN)),
                "original_list_usd": _num(row_value(values, ORIGINAL_PRICE_USD_COLUMN)),
                "price_owner": _text(row_value(values, PRICE_OWNER_COLUMN)),
                "events": [],
            })

        original = [dict(x) for x in states]
        items: list[BatchPreviewItem] = []
        floor_issues: list[CostFloorIssue] = []

        for c, action in ordered:
            action = action.upper()
            matched = changed = below = 0
            collisions: list[str] = []
            for idx, values in enumerate(rows):
                if not _matches(values, h, c):
                    continue
                matched += 1
                state = states[idx]
                current = _text(state["promo_code"])
                iso = _text(row_value(values, "ISO"))
                plan = _text(row_value(values, "Plan"))
                day = int(_num(row_value(values, "Days")) or 0)

                if action == "END":
                    if current == c.promo_code:
                        # If this planner temporarily raised list prices when the promo
                        # started, restore the exact pre-campaign prices at END.
                        if state.get("price_owner") == c.promo_code:
                            if state.get("original_list_eur") is not None:
                                state["list_eur"] = state["original_list_eur"]
                            if state.get("original_list_usd") is not None:
                                state["list_usd"] = state["original_list_usd"]
                            state["original_list_eur"] = None
                            state["original_list_usd"] = None
                            state["price_owner"] = ""
                        state["promo_code"] = ""
                        state["promo_type"] = ""
                        state["promo_value"] = 0.0
                        state["promo_label"] = ""
                        state["events"].append(f"END {c.name}")
                        changed += 1

                elif action == "START":
                    if current and current != c.promo_code:
                        collisions.append(f"{c.name}: {iso} / {plan} / {day}d already has {current}")
                        continue
                    if current != c.promo_code:
                        changed += 1
                    row_below = False
                    for currency in ("EUR", "USD"):
                        list_key = "list_eur" if currency == "EUR" else "list_usd"
                        fcol = f"CostFloor_{currency}"
                        base = state[list_key]
                        floor = _num(row_value(values, fcol)) if fcol in h else None
                        if base is None or floor is None:
                            continue
                        net = round_promo_price(base, c.promo_type, c.promo_value)
                        if net < floor - 1e-9:
                            row_below = True
                            target = float(floor) * 1.10
                            required = required_list_price_for_target(target, c.promo_type, c.promo_value)
                            floor_issues.append(CostFloorIssue(
                                int(c.id or 0), c.name, iso, plan, day, currency,
                                float(base), float(net), float(floor), float(target), float(required)
                            ))
                            if adjust_below_floor_to_margin and base < required - 1e-9:
                                original_key = "original_list_eur" if currency == "EUR" else "original_list_usd"
                                if state.get(original_key) is None:
                                    state[original_key] = float(base)
                                if not state.get("price_owner"):
                                    state["price_owner"] = c.promo_code
                                state[list_key] = float(required)
                    if row_below:
                        below += 1
                    state["promo_code"] = c.promo_code
                    state["promo_type"] = c.promo_type
                    state["promo_value"] = float(c.promo_value)
                    state["promo_label"] = c.promo_name or c.promo_code
                    state["events"].append(f"START {c.name}")
                else:
                    raise ValueError(f"Unsupported batch action: {action}")
            items.append(BatchPreviewItem(int(c.id or 0), c.name, action, matched, changed, below, collisions[:100]))

        changes: list[PriceChange] = []
        for idx, values in enumerate(rows):
            before = original[idx]
            after = states[idx]
            if not after["events"]:
                continue

            def net(state, currency):
                base = state["list_eur"] if currency == "EUR" else state["list_usd"]
                if base is None:
                    return None
                if state["promo_code"]:
                    return round_promo_price(base, state["promo_type"], state["promo_value"])
                return round(float(base), 2)

            iso = _text(row_value(values, "ISO"))
            country = _text(row_value(values, "Country")) or iso
            changes.append(PriceChange(
                iso=iso,
                country=country,
                plan=_text(row_value(values, "Plan")),
                days=int(_num(row_value(values, "Days")) or 0),
                change=" → ".join(after["events"]),
                promo_before=before["promo_label"] or before["promo_code"] or "—",
                promo_after=after["promo_label"] or after["promo_code"] or "—",
                list_eur_before=before["list_eur"],
                list_eur_after=after["list_eur"],
                net_eur_before=net(before, "EUR"),
                net_eur_after=net(after, "EUR"),
                list_usd_before=before["list_usd"],
                list_usd_after=after["list_usd"],
                net_usd_before=net(before, "USD"),
                net_usd_after=net(after, "USD"),
            ))

        return BatchPreview(execution_date, items, floor_issues, changes)
    finally:
        wb.close()


def execute_batch(
    path: Path,
    actions: list[tuple[Campaign, str]],
    backup_dir: Path,
    execution_date: str,
    *,
    adjust_below_floor_to_margin: bool = False,
) -> tuple[Path, dict[tuple[int, str], int], int]:
    ordered = _ordered_actions(actions)
    preview = preview_batch(path, ordered, execution_date, adjust_below_floor_to_margin=adjust_below_floor_to_margin)
    if preview.collisions:
        raise RuntimeError("Promo collision detected. Resolve before execution:\n" + "\n".join(preview.collisions[:30]))
    by_id = {int(c.id or 0): c for c, _ in ordered}
    blocked = [i for i in preview.items if i.action == "START" and i.below_floor_rows and not by_id[i.campaign_id].allow_below_cost]
    if blocked and not adjust_below_floor_to_margin:
        lines = [f"{i.campaign_name}: {i.below_floor_rows} rows below cost floor" for i in blocked]
        raise RuntimeError("Batch blocked by cost floor:\n" + "\n".join(lines))

    # Map exact affected pricing points/currencies to the required safe list price.
    adjustment_map: dict[tuple[int, str, str, int, str], float] = {}
    if adjust_below_floor_to_margin:
        for issue in preview.cost_floor_issues:
            key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency)
            adjustment_map[key] = max(adjustment_map.get(key, 0.0), issue.required_list_price)

    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{path.stem}_before_batch_{execution_date}_{ts}{path.suffix}"
    shutil.copy2(path, backup)

    wb = load_workbook(path, keep_vba=path.suffix.lower() == ".xlsm")
    changed_map: dict[tuple[int, str], int] = {}
    adjusted_cells = 0
    try:
        ws = wb[PRICING_SHEET] if PRICING_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        h = _ensure_promo_name_column(ws, _headers(ws))
        h = _ensure_planner_price_columns(ws, h)
        for c, action in ordered:
            action = action.upper()
            changed = 0
            for r in range(2, ws.max_row + 1):
                vals = [ws.cell(r, col).value for col in range(1, ws.max_column + 1)]
                if not _matches(vals, h, c):
                    continue
                promo_cell = ws.cell(r, h["PromoCode"])
                name_cell = ws.cell(r, h[PROMO_NAME_COLUMN])
                current = _text(promo_cell.value)
                iso = _text(ws.cell(r, h["ISO"]).value).upper()
                plan = _text(ws.cell(r, h["Plan"]).value)
                day = int(_num(ws.cell(r, h["Days"]).value) or 0)
                if action == "END" and current == c.promo_code:
                    owner = _text(ws.cell(r, h[PRICE_OWNER_COLUMN]).value)
                    if owner == c.promo_code:
                        original_eur = _num(ws.cell(r, h[ORIGINAL_PRICE_EUR_COLUMN]).value)
                        original_usd = _num(ws.cell(r, h[ORIGINAL_PRICE_USD_COLUMN]).value)
                        if original_eur is not None and "Price_EUR" in h:
                            ws.cell(r, h["Price_EUR"]).value = original_eur
                        if original_usd is not None and "Price_USD" in h:
                            ws.cell(r, h["Price_USD"]).value = original_usd
                        ws.cell(r, h[ORIGINAL_PRICE_EUR_COLUMN]).value = None
                        ws.cell(r, h[ORIGINAL_PRICE_USD_COLUMN]).value = None
                        ws.cell(r, h[PRICE_OWNER_COLUMN]).value = None
                    promo_cell.value = ""
                    name_cell.value = ""
                    if "AllowBelowCost" in h:
                        ws.cell(r, h["AllowBelowCost"]).value = False
                    changed += 1
                elif action == "START":
                    if current and current != c.promo_code:
                        raise RuntimeError(f"Collision while applying {c.name}: pricing row already has {current}")

                    row_adjusted = False
                    if adjust_below_floor_to_margin:
                        for currency in ("EUR", "USD"):
                            key = (int(c.id or 0), iso, plan, day, currency)
                            required = adjustment_map.get(key)
                            price_col = f"Price_{currency}"
                            if required is not None and price_col in h:
                                cell = ws.cell(r, h[price_col])
                                current_price = _num(cell.value)
                                if current_price is None or current_price < required - 1e-9:
                                    original_col = ORIGINAL_PRICE_EUR_COLUMN if currency == "EUR" else ORIGINAL_PRICE_USD_COLUMN
                                    owner_cell = ws.cell(r, h[PRICE_OWNER_COLUMN])
                                    original_cell = ws.cell(r, h[original_col])
                                    if _text(owner_cell.value) not in {"", c.promo_code}:
                                        raise RuntimeError(
                                            f"Temporary list-price owner conflict for {iso} / {plan} / {day}d: "
                                            f"{owner_cell.value}"
                                        )
                                    if _num(original_cell.value) is None and current_price is not None:
                                        original_cell.value = current_price
                                    owner_cell.value = c.promo_code
                                    cell.value = required
                                    adjusted_cells += 1
                                row_adjusted = True

                    if current != c.promo_code:
                        promo_cell.value = c.promo_code
                        name_cell.value = c.promo_name
                        changed += 1
                    elif _text(name_cell.value) != c.promo_name:
                        name_cell.value = c.promo_name

                    if "AllowBelowCost" in h:
                        # If the row was lifted to the safe margin, no override is needed.
                        ws.cell(r, h["AllowBelowCost"]).value = bool(c.allow_below_cost and not row_adjusted)
            changed_map[(int(c.id or 0), action)] = changed
        try:
            wb.calculation.fullCalcOnLoad = True
            wb.calculation.forceFullCalc = True
            wb.calculation.calcMode = "auto"
        except Exception:
            pass
        wb.save(path)
    except Exception:
        try:
            wb.close()
        except Exception:
            pass
        shutil.copy2(backup, path)
        raise
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return backup, changed_map, adjusted_cells


def restore_workbook_backup(path: Path, backup: Path) -> None:
    if not backup.exists():
        raise FileNotFoundError(f"Backup not found: {backup}")
    shutil.copy2(backup, path)
