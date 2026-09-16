
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPolygonF
from PySide6.QtWidgets import QWidget #, QToolTip

import math

try:
    from plan_labels import display_plan_label
except ImportError:
    from automation.plan_labels import display_plan_label


class PriceCurveCanvas(QWidget):
    pointSelected = Signal(str)
    pointDragged = Signal(str, float, int)
    recommendationSelected = Signal(str)
    promoSelected = Signal(str)
    statusChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(1040, 650)
        self.setMouseTracking(True)
        self.competitors: list[dict[str, Any]] = []
        self.points: list[dict[str, Any]] = []
        self.promo_markers: list[dict[str, Any]] = []
        self.selected_row_id: str | None = None
        self.drag_index: int | None = None
        self.margin_left = 70
        self.margin_right = 50
        self.margin_top = 55
        self.margin_bottom = 50
        self.provider_shapes = ["circle", "square", "triangle", "diamond", "cross", "pentagon"]
        self.fixed_provider_shapes = {
            "holafly": "triangle-down",
            "saily": "diamond",
            "orange": "pentagon",
            "vodafone": "triangle",
            "vodafoen": "triangle",
            "airalo": "square",
        }
        self.zoom_x_min = None
        self.zoom_x_max = None
        self.zoom_y_min = None
        self.zoom_y_max = None
        self.competitor_hitboxes = []
        self.recommendation_hitboxes = []
        self.recommendation_card_apply_hitbox: QRectF | None = None
        self.recommendation_card_row_id: str | None = None
        self.zoom_rect = None
        self.zoom_start = None
        self.pan_start = None
        self.pan_origin = None
        self.is_dragging = False
        self.show_promo_markers = False
        self.show_prices = True
        self.setFocusPolicy(Qt.StrongFocus)
        self.show_competitors = True
        self.title = ""

    def set_data(self, competitors: list[dict[str, Any]], points: list[dict[str, Any]], promo_markers: list[dict[str, Any]], selected_row_id: str | None, title="") -> None:
        self.competitors = competitors
        self.points = points
        self.promo_markers = promo_markers
        self.selected_row_id = selected_row_id
        self.title = title
        self.update()
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Q:
            self.show_prices = not self.show_prices
            self.update()
            return
        
        if event.key() == Qt.Key_S:
            self.show_competitors = not self.show_competitors
            self.update()
            return
        
        if event.key() == Qt.Key_H:
            self.reset_zoom()
            return

        super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        # --- Pan ---
        if self.pan_start is not None and self.pan_origin is not None:
            x0, x1, y0, y1 = self.pan_origin
            dx0, dy0 = self._from_screen(self.pan_start.x(), self.pan_start.y())
            dx1, dy1 = self._from_screen(event.position().x(), event.position().y())
            shift_x = dx0 - dx1
            shift_y = dy0 - dy1
            self.zoom_x_min = x0 + shift_x
            self.zoom_x_max = x1 + shift_x
            self.zoom_y_min = y0 + shift_y
            self.zoom_y_max = y1 + shift_y
            self.setCursor(Qt.SizeAllCursor)
            self.update()
            return

        # --- Zoom drag ---
        if self.zoom_start is not None:
            self.setCursor(Qt.CrossCursor)
            self.zoom_rect = (self.zoom_start, event.position())
            self.update()
            return

        # --- Protected price dragging: SHIFT must remain pressed ---
        if self.drag_index is not None and 0 <= self.drag_index < len(self.points):
            if not (event.modifiers() & Qt.ShiftModifier):
                self.drag_index = None
                self.is_dragging = False
                self.setCursor(Qt.ArrowCursor)
                self.statusChanged.emit("Price edit stopped: hold SHIFT while dragging curve points.")
                return
            self.setCursor(Qt.SizeVerCursor)
            y = self._from_screen_y(event.position().y())
            row_id = self.points[self.drag_index]["row_id"]
            self.pointDragged.emit(row_id, y, self.drag_index)
            return

        if (
            self.recommendation_card_apply_hitbox is not None
            and self.recommendation_card_row_id
            and self.recommendation_card_apply_hitbox.contains(event.position())
        ):
            self.setCursor(Qt.PointingHandCursor)
            self.statusChanged.emit("Apply the selected SKU recommendation.")
            return

        recommendation_hit = self._nearest_recommendation(event.position())
        if recommendation_hit is not None:
            _, recommendation = recommendation_hit
            self.setCursor(Qt.PointingHandCursor)
            self.statusChanged.emit(self._recommendation_message(recommendation))
            return

        promo_code = self._nearest_promo_marker(event.position())
        if promo_code:
            self.setCursor(Qt.PointingHandCursor)
            self.statusChanged.emit(f"Promo option: {promo_code}")
            return

        # Competitor hover before HT point hover.
        comp = self._nearest_competitor(event.position()) if self.show_competitors else None
        if comp is not None:
            self.setCursor(Qt.ArrowCursor)
            msg = (
                f'Operator: {comp["provider"]} | Plan: {display_plan_label(comp.get("plan", ""))} | '
                f'Days: {comp.get("days", "")} | GB: {comp.get("gb", "")} | '
                f'Price: {comp["y"]:.2f} | Promo: no'
            )
            self.statusChanged.emit(msg)
            return

        idx = self._nearest_point_index(event.position())
        if idx is not None:
            self.setCursor(Qt.ArrowCursor)
            p = self.points[idx]
            msg = (
                f'Operator: HT | Plan: {display_plan_label(p["plan"])} | Days: {p["days"]} | '
                f'GB: {p["gb"]} | Price: {p["y"]:.2f} | Promo: {p["promo"] or "no"} | '
                f'Hold SHIFT + drag to edit'
            )
            self.statusChanged.emit(msg)
            return

        self.setCursor(Qt.ArrowCursor)
        self.statusChanged.emit("")

    def mouseReleaseEvent(self, event):

         # --- Finish pan ---
        if self.pan_start is not None:
            self.pan_start = None
            self.pan_origin = None
            self.setCursor(Qt.ArrowCursor)
            return

        # --- Finish zoom ---
        if self.zoom_start is not None:
            p1 = self.zoom_start
            p2 = event.position()

            x1, y1 = self._from_screen(p1.x(), p1.y())
            x2, y2 = self._from_screen(p2.x(), p2.y())

            self.zoom_x_min = min(x1, x2)
            self.zoom_x_max = max(x1, x2)
            self.zoom_y_min = min(y1, y2)
            self.zoom_y_max = max(y1, y2)

            self.zoom_start = None
            self.zoom_rect = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            return

        # --- Stop dragging ---
        self.drag_index = None
        self.is_dragging = False
        self.setCursor(Qt.ArrowCursor)
    
    def reset_zoom(self):
        self.zoom_x_min = self.zoom_x_max = self.zoom_y_min = self.zoom_y_max = None
        self.update()

    def _provider_shape(self, provider: str) -> str:
        key = str(provider).strip().lower()
        if key in self.fixed_provider_shapes:
            return self.fixed_provider_shapes[key]
        providers = sorted({str(c.get("provider", "")).strip() or "Market" for c in self.competitors})
        idx = providers.index(str(provider).strip() or "Market") if (str(provider).strip() or "Market") in providers else 0
        return self.provider_shapes[idx % len(self.provider_shapes)]

    def _gb_fill(self, gb):
        if gb is None or (isinstance(gb, float) and math.isnan(gb)):
            return QColor(255, 245, 245)
        v = max(0.0, min(float(gb), 100.0))
        r = 255
        g = int(245 - v * 1.7)
        b = int(245 - v * 1.9)
        return QColor(r, max(60, g), max(60, b))

    def _is_unlimited(self, plan: str) -> bool:
        return "unlimited" in str(plan).strip().lower()
    
    def _is_below_cost_floor(self, point: dict[str, Any]) -> bool:
        if "is_below_cost_floor" in point:
            return bool(point.get("is_below_cost_floor"))
        floor = point.get("cost_floor")
        if floor is None:
            return False

        try:
            return float(point.get("y", 0.0)) < float(floor)
        except Exception:
            return False

    def _floor_marker_state(self, point: dict[str, Any]) -> str:
        below_by_currency = point.get("below_cost_floor_by_currency") or {}
        active_currency = str(point.get("active_currency") or "").upper()
        active_below = self._is_below_cost_floor(point)
        other_below = any(
            bool(is_below)
            for currency, is_below in below_by_currency.items()
            if str(currency).upper() != active_currency
        )
        if active_below and other_below:
            return "both_below"
        if active_below:
            return "active_below"
        if other_below:
            return "other_below"
        return "ok"

    def _data_ranges(self):
        xs = [p["x"] for p in self.points] + [c["x"] for c in self.competitors]

        if not xs:
            xs = [0.0, 1.0]

        x0, x1 = min(xs), max(xs)

        # Use current HT prices and visible cost-floor values for chart scaling.
        ht_prices = [float(p["y"]) for p in self.points]
        ht_prices.extend(
            float(p["cost_floor"])
            for p in self.points
            if p.get("cost_floor") is not None
        )

        if not ht_prices:
            ht_prices = [0.0, 1.0]

        y0 = 0.0

        # Preferred max = Unlimited 30d HT price
        unlimited_30 = [
            float(p["y"])
            for p in self.points
            if self._is_unlimited(p.get("plan", ""))
            and float(p.get("days", 0)) == 30
        ]

        if unlimited_30:
            y1 = max(unlimited_30)
        else:
            y1 = max(ht_prices)

        if math.isclose(x0, x1):
            x1 = x0 + 1.0

        if math.isclose(y0, y1):
            y1 = y0 + 1.0

        pad_y = max(10.0, y1 * 0.20)
        pad_x = (x1 - x0) * 0.08

        return x0 - pad_x, x1 + pad_x, 0.0, y1 + pad_y

    def _ranges(self):
        x0, x1, y0, y1 = self._data_ranges()
        if None not in (self.zoom_x_min, self.zoom_x_max, self.zoom_y_min, self.zoom_y_max):
            return self.zoom_x_min, self.zoom_x_max, self.zoom_y_min, self.zoom_y_max
        return x0, x1, y0, y1

    def _plot_rect(self) -> QRectF:
        return QRectF(self.margin_left, self.margin_top, max(10.0, self.width() - self.margin_left - self.margin_right), max(10.0, self.height() - self.margin_top - self.margin_bottom))

    def _to_screen(self, x: float, y: float) -> QPointF:
        rect = self._plot_rect()
        x0, x1, y0, y1 = self._ranges()

        if math.isclose(x0, x1):
            x1 = x0 + 1.0
        if math.isclose(y0, y1):
            y1 = y0 + 1.0

        sx = rect.left() + (x - x0) / (x1 - x0) * rect.width()
        sy = rect.bottom() - (y - y0) / (y1 - y0) * rect.height()
        return QPointF(float(sx), float(sy))

    def _from_screen(self, sx: float, sy: float) -> tuple[float, float]:
        rect = self._plot_rect()
        x0, x1, y0, y1 = self._ranges()
        xr = (sx - rect.left()) / rect.width()
        yr = (rect.bottom() - sy) / rect.height()
        return float(x0 + xr * (x1 - x0)), float(y0 + yr * (y1 - y0))

    def _from_screen_y(self, sy: float) -> float:
        rect = self._plot_rect()
        y0, y1 = self._ranges()[2], self._ranges()[3]

        # Prevent dragging outside visible chart area.
        sy = max(rect.top(), min(float(sy), rect.bottom()))

        _, y = self._from_screen(rect.left(), sy)

        return max(y0, min(y, y1))

    def _nearest_point_index(self, pos) -> int | None:
        best_idx, best_dist = None, 999999.0
        for i, point in enumerate(self.points):
            pt = self._to_screen(point["x"], point["y"])
            dist = math.hypot(pos.x() - pt.x(), pos.y() - pt.y())
            if dist < best_dist:
                best_dist, best_idx = dist, i
        return best_idx if best_dist <= 15 else None

    def _nearest_competitor(self, pos):
        best = None
        best_dist = 999999.0
        for x, y, item in self.competitor_hitboxes:
            dist = ((pos.x() - x) ** 2 + (pos.y() - y) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = item
        return best if best_dist <= 26 else None

    def _nearest_promo_marker(self, pos) -> str | None:
        best, best_dist = None, 999999.0
        for item in self.promo_markers:
            pt = self._to_screen(item["x"], item["y"])
            dist = math.hypot(pos.x() - pt.x(), pos.y() - pt.y())
            if dist < best_dist:
                best_dist, best = dist, item["promo_code"]
        return best if best_dist <= 13 else None

    def _nearest_recommendation(self, pos):
        for rect, row_id, recommendation in reversed(self.recommendation_hitboxes):
            if rect.contains(pos):
                return row_id, recommendation
        return None

    @staticmethod
    def _rec_num(recommendation: dict[str, Any], key: str) -> float | None:
        try:
            value = float(recommendation.get(key))
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _recommendation_kind(mechanism: str) -> tuple[str, str]:
        mechanism = str(mechanism or "").upper()
        if mechanism == "PROMO":
            return "P", "PROMO / NET PRICE"
        if mechanism.startswith("LIST_PRICE"):
            return "L", "LIST PRICE"
        return "?", mechanism.replace("_", " ") or "UNKNOWN"

    @staticmethod
    def _market_match_text(recommendation: dict[str, Any]) -> str:
        mode = str(recommendation.get("MarketMatchMode", "") or "").strip()
        days = recommendation.get("Days")
        try:
            days_text = f"{int(round(float(days)))}d"
        except Exception:
            days_text = "same duration"

        if mode == "exact_day":
            duration_text = f"exact {days_text}"
        elif mode.startswith("fallback_"):
            dmin = recommendation.get("MarketDurationMin")
            dmax = recommendation.get("MarketDurationMax")
            try:
                duration_text = f"fallback {float(dmin):g}-{float(dmax):g}d"
            except Exception:
                duration_text = mode.replace("fallback_", "fallback ").replace("_", "-")
        else:
            duration_text = "market match unavailable"

        plan = str(recommendation.get("Plan", "") or "").lower()
        if "unlimited" in plan:
            metric_text = "Unlimited only · total price"
        else:
            weight = recommendation.get("MarketPricePerGBWeight")
            try:
                ppg = float(weight)
                metric_text = f"capped only · {ppg:.0%} price/GB + {1.0-ppg:.0%} total"
            except Exception:
                metric_text = "capped only · price/GB + total"
        return f"{duration_text} · {metric_text}"

    def _recommendation_message(self, recommendation: dict[str, Any]) -> str:
        direction = str(recommendation.get("Direction", "")).upper()
        confidence = str(recommendation.get("Confidence", "")).upper()
        mechanism = str(recommendation.get("Mechanism", "")).upper()
        kind, mechanism_label = self._recommendation_kind(mechanism)
        currency = str(recommendation.get("Currency", ""))
        current_list = self._rec_num(recommendation, "CurrentListPriceNow")
        if current_list is None:
            current_list = self._rec_num(recommendation, "CurrentListPrice")
        current_net = self._rec_num(recommendation, "CurrentNetPriceNow")
        if current_net is None:
            current_net = self._rec_num(recommendation, "CurrentNetPrice")
        market_target = self._rec_num(recommendation, "MarketTargetPrice")

        if kind == "L":
            suggested = self._rec_num(recommendation, "SuggestedListPrice")
            deviation = self._rec_num(recommendation, "ListPositionDeviationPct")
        else:
            suggested = self._rec_num(recommendation, "SuggestedPromoFinalPrice")
            if suggested is None:
                suggested = self._rec_num(recommendation, "SuggestedNetPrice")
            deviation = self._rec_num(recommendation, "PositionDeviationPct")

        suggested_text = f"{suggested:.2f} {currency}" if suggested is not None else "-"
        current_text = []
        if current_list is not None:
            current_text.append(f"list {current_list:.2f}")
        if current_net is not None:
            current_text.append(f"net {current_net:.2f}")
        market_text = f" | market ref {market_target:.2f}" if market_target is not None else ""
        deviation_text = f" | vs neighbouring anchors {deviation:+.0%}" if deviation is not None else ""
        promo_code = str(recommendation.get("SuggestedPromoCode", "")).strip()
        if promo_code.lower() in {"nan", "none"}:
            promo_code = ""
        promo_text = f" via {promo_code}" if kind == "P" and promo_code else ""
        match_text = self._market_match_text(recommendation)
        return (
            f"{('▲' if direction == 'UP' else '▼')}{kind} {direction} {mechanism_label}: "
            f"{' / '.join(current_text)} -> {suggested_text}{promo_text}{market_text}{deviation_text} "
            f"| {match_text} | {confidence} | click to apply this SKU only"
        )

    def _draw_recommendation_arrow(
        self,
        painter: QPainter,
        point_screen: QPointF,
        direction: str,
        confidence: str,
        mechanism: str = "",
    ) -> QRectF:
        direction = str(direction).upper()
        confidence = str(confidence).upper()
        kind, _ = self._recommendation_kind(mechanism)
        size = 6 if confidence == "HIGH" else 5
        cx = point_screen.x() + 20
        cy = point_screen.y()
        color = QColor("#2e7d32") if direction == "UP" else QColor("#c62828")
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(color))
        if direction == "UP":
            polygon = QPolygonF([
                QPointF(cx, cy - size),
                QPointF(cx - size, cy + size),
                QPointF(cx + size, cy + size),
            ])
        else:
            polygon = QPolygonF([
                QPointF(cx - size, cy - size),
                QPointF(cx + size, cy - size),
                QPointF(cx, cy + size),
            ])
        painter.drawPolygon(polygon)

        old_font = painter.font()
        tag_font = QFont(old_font)
        tag_font.setPointSize(max(7, old_font.pointSize() - 1))
        tag_font.setBold(True)
        painter.setFont(tag_font)
        painter.setPen(color)
        painter.drawText(int(cx + 8), int(cy + 4), kind)
        painter.setFont(old_font)
        return QRectF(cx - 9, cy - 10, 28, 20)

    @staticmethod
    def _recommendation_reason_short(recommendation: dict[str, Any]) -> str:
        reason = str(recommendation.get("Reason", "") or "").strip().lower()
        direction = str(recommendation.get("Direction", "") or "").upper()
        if reason == "local_signal_list_price":
            side = "cheap" if direction == "UP" else "expensive"
            return (
                f"Permanent list price looks locally {side} versus adjacent duration anchors "
                "after competitor adjustment."
            )
        if reason == "isolated_anchor_local_signal":
            side = "cheap" if direction == "UP" else "expensive"
            return (
                f"This is an isolated {side} anchor, so the recommendation uses a promo/net-price "
                "adjustment instead of reshaping the list-price curve."
            )
        if reason == "consecutive_anchor_local_signal":
            side = "low" if direction == "UP" else "high"
            return f"The same structural signal appears across consecutive anchors; this curve segment looks {side}."
        raw = str(recommendation.get("Reason", "") or "").replace("_", " ").strip()
        return raw or "Local market-position signal versus neighbouring anchors."

    def _selected_point(self) -> dict[str, Any] | None:
        if self.selected_row_id is None:
            return None
        selected = str(self.selected_row_id)
        for point in self.points:
            if str(point.get("row_id", "")) == selected:
                return point
        return None

    def _draw_recommendation_card(self, painter: QPainter, plot_rect: QRectF, x: float, y: float) -> float:
        """Draw a persistent explanation card for the selected anchor.

        The card intentionally lives inside the chart below the legend.  The
        only interactive element is the Apply button; clicking elsewhere on
        the card does not change prices.
        """
        point = self._selected_point()
        if point is None:
            return y

        recommendation = point.get("recommendation")
        stale = bool(point.get("recommendation_stale", False))
        applied = bool(point.get("recommendation_applied", False))

        max_width = max(260.0, min(470.0, float(plot_rect.width()) * 0.42))
        card_x = float(x)
        card_width = min(max_width, float(plot_rect.right()) - card_x - 8.0)
        if card_width < 240:
            return y

        old_font = painter.font()

        # A selected point with no currently actionable recommendation still
        # gets a small persistent explanation instead of appearing to do nothing.
        if not recommendation:
            if stale:
                message = "Recommendation is stale - rerun pricing_recommendations.py."
                border = QColor("#ef6c00")
            elif applied:
                message = "Recommendation applied in this session."
                border = QColor("#2e7d32")
            else:
                message = "No recommendation for this anchor."
                border = QColor("#9e9e9e")

            card_h = 42.0
            card_rect = QRectF(card_x, y, card_width, card_h)
            painter.setPen(QPen(border, 1))
            painter.setBrush(QBrush(QColor(255, 255, 255, 232)))
            painter.drawRoundedRect(card_rect, 6, 6)
            painter.setPen(QColor("#444444"))
            small_font = QFont(old_font)
            small_font.setPointSize(8)
            painter.setFont(small_font)
            painter.drawText(card_rect.adjusted(10, 7, -10, -7), Qt.AlignLeft | Qt.AlignVCenter, message)
            painter.setFont(old_font)
            return card_rect.bottom()

        direction = str(recommendation.get("Direction", "") or "").upper()
        confidence = str(recommendation.get("Confidence", "") or "").upper()
        mechanism = str(recommendation.get("Mechanism", "") or "").upper()
        kind, mechanism_label = self._recommendation_kind(mechanism)
        currency = str(recommendation.get("Currency", "") or "")
        accent = QColor("#2e7d32") if direction == "UP" else QColor("#c62828")
        arrow = "▲" if direction == "UP" else "▼"

        current_list = self._rec_num(recommendation, "CurrentListPriceNow")
        if current_list is None:
            current_list = self._rec_num(recommendation, "CurrentListPrice")
        current_net = self._rec_num(recommendation, "CurrentNetPriceNow")
        if current_net is None:
            current_net = self._rec_num(recommendation, "CurrentNetPrice")
        market_target = self._rec_num(recommendation, "MarketTargetPrice")

        if kind == "L":
            suggested = self._rec_num(recommendation, "SuggestedListPrice")
            deviation = self._rec_num(recommendation, "ListPositionDeviationPct")
        else:
            suggested = self._rec_num(recommendation, "SuggestedPromoFinalPrice")
            if suggested is None:
                suggested = self._rec_num(recommendation, "SuggestedNetPrice")
            deviation = self._rec_num(recommendation, "PositionDeviationPct")

        current_parts = []
        if current_list is not None:
            current_parts.append(f"{current_list:.2f} list")
        if current_net is not None:
            current_parts.append(f"{current_net:.2f} net")
        current_text = " / ".join(current_parts) if current_parts else "current price unavailable"
        suggested_text = f"{suggested:.2f} {currency}" if suggested is not None else "-"

        market_parts = []
        if market_target is not None:
            market_parts.append(f"market ref {market_target:.2f} {currency}")
        if deviation is not None:
            market_parts.append(f"vs neighbouring anchors {deviation:+.0%}")
        providers = self._rec_num(recommendation, "MarketProviderCount")
        if providers is not None:
            market_parts.append(f"{int(providers)} providers")
        market_line = " · ".join(market_parts) if market_parts else "Local market / neighbour comparison"

        countries = str(recommendation.get("Countries", "") or "").strip()
        unit = str(recommendation.get("PricingUnitIdUsed", "") or "").strip()
        priority = str(recommendation.get("PriorityCountry", "") or "").strip()
        if countries and "," in countries:
            scope = f"this anchor SKU across shared unit {unit or countries} ({countries})"
            if priority and priority.lower() not in {"nan", "none"}:
                scope += f" · priority market {priority}"
        else:
            scope = "this anchor SKU only"

        promo_code = str(recommendation.get("CurrentPromoCodeNow", recommendation.get("CurrentPromoCode", "")) or "").strip()
        if promo_code.lower() in {"nan", "none"}:
            promo_code = ""
        promo_note = f" · active promo {promo_code}" if promo_code else ""
        action_prefix = "List" if kind == "L" else "Net"

        # Fixed-height compact card.  It also exposes exactly how the market
        # neighbours were selected so the recommendation can be audited.
        card_h = 180.0
        if y + card_h > plot_rect.bottom() - 6:
            # Keep the card visible on shorter windows while remaining aligned
            # with the legend column.
            y = max(float(plot_rect.top()) + 8.0, float(plot_rect.bottom()) - card_h - 6.0)
        card_rect = QRectF(card_x, y, card_width, card_h)
        painter.setPen(QPen(accent, 1.5))
        painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawRoundedRect(card_rect, 7, 7)

        header_font = QFont(old_font)
        header_font.setPointSize(9)
        header_font.setBold(True)
        painter.setFont(header_font)
        painter.setPen(accent)
        painter.drawText(
            QRectF(card_x + 10, y + 7, card_width - 20, 20),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{arrow}{kind}  {direction} {mechanism_label} · {confidence}",
        )

        body_font = QFont(old_font)
        body_font.setPointSize(8)
        body_font.setBold(False)
        painter.setFont(body_font)
        painter.setPen(QColor("#333333"))
        painter.drawText(
            QRectF(card_x + 10, y + 30, card_width - 20, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Current: {current_text}{promo_note}  ->  {action_prefix}: {suggested_text}",
        )
        painter.drawText(
            QRectF(card_x + 10, y + 49, card_width - 20, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            market_line,
        )
        match_line = self._market_match_text(recommendation)
        painter.setPen(QColor("#555555"))
        painter.drawText(
            QRectF(card_x + 10, y + 67, card_width - 20, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Match: {match_line}",
        )

        painter.setPen(QColor("#333333"))
        reason = self._recommendation_reason_short(recommendation)
        painter.drawText(
            QRectF(card_x + 10, y + 88, card_width - 20, 38),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            f"Why: {reason}",
        )
        painter.setPen(QColor("#555555"))
        painter.drawText(
            QRectF(card_x + 10, y + 128, card_width - 145, 36),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            f"Scope: {scope}. It does not move the rest of the curve.",
        )

        button_w = 122.0
        button_h = 26.0
        button_rect = QRectF(card_rect.right() - button_w - 10, card_rect.bottom() - button_h - 10, button_w, button_h)
        painter.setPen(QPen(accent, 1))
        painter.setBrush(QBrush(QColor(250, 250, 250, 245)))
        painter.drawRoundedRect(button_rect, 5, 5)
        button_font = QFont(body_font)
        button_font.setBold(True)
        painter.setFont(button_font)
        painter.setPen(accent)
        painter.drawText(button_rect, Qt.AlignCenter, "Apply recommendation")

        self.recommendation_card_apply_hitbox = button_rect
        self.recommendation_card_row_id = str(point.get("row_id", ""))
        painter.setFont(old_font)
        return card_rect.bottom()

    def wheelEvent(self, event):
        rect = self._plot_rect()
        if not rect.contains(event.position()):
            return
        x0, x1, y0, y1 = self._ranges()
        data_x, data_y = self._from_screen(event.position().x(), event.position().y())
        factor = 0.85 if event.angleDelta().y() > 0 else 1.18
        new_x0 = data_x - (data_x - x0) * factor
        new_x1 = data_x + (x1 - data_x) * factor
        new_y0 = data_y - (data_y - y0) * factor
        new_y1 = data_y + (y1 - data_y) * factor
        self.zoom_x_min, self.zoom_x_max, self.zoom_y_min, self.zoom_y_max = new_x0, new_x1, new_y0, new_y1
        self.update()

    def mouseDoubleClickEvent(self, event):
        self.reset_zoom()

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MiddleButton:
            self.pan_start = event.position()
            self.pan_origin = self._ranges()
            return

        if event.button() == Qt.RightButton:
            promo_code = self._nearest_promo_marker(event.position())
            if promo_code:
                self.promoSelected.emit(promo_code)
                return

            idx = self._nearest_point_index(event.position())
            if idx is not None:
                row_id = self.points[idx]["row_id"]
                self.selected_row_id = row_id
                self.show_promo_markers = True
                self.pointSelected.emit(row_id)
                return

            self.zoom_start = event.position()
            self.zoom_rect = None
            return

        if event.button() == Qt.LeftButton:
            if (
                self.recommendation_card_apply_hitbox is not None
                and self.recommendation_card_row_id
                and self.recommendation_card_apply_hitbox.contains(event.position())
            ):
                self.recommendationSelected.emit(str(self.recommendation_card_row_id))
                return

            recommendation_hit = self._nearest_recommendation(event.position())
            if recommendation_hit is not None:
                row_id, _ = recommendation_hit
                self.selected_row_id = str(row_id)
                self.show_promo_markers = False
                self.promo_markers = []
                self.recommendationSelected.emit(str(row_id))
                return

            idx = self._nearest_point_index(event.position())
            if idx is not None:
                self.show_promo_markers = False
                self.promo_markers = []
                row_id = self.points[idx]["row_id"]
                self.selected_row_id = row_id

                # Selection is always safe. Price editing only starts while SHIFT is held.
                if event.modifiers() & Qt.ShiftModifier:
                    self.drag_index = idx
                    self.is_dragging = True
                    self.statusChanged.emit("Protected edit active: keep SHIFT pressed while dragging.")
                else:
                    self.drag_index = None
                    self.is_dragging = False
                    self.statusChanged.emit("Point selected. Hold SHIFT + drag to edit with the selected curve tool.")

                self.pointSelected.emit(row_id)
                self.update()
                return


    def _draw_polyline(self, painter: QPainter, pts: list[dict[str, Any]], color: QColor, width: int, dashed: bool = False):
        if len(pts) < 2:
            return
        screen_pts = [self._to_screen(p["x"], p["y"]) for p in pts]
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if dashed:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        for i in range(len(screen_pts) - 1):
            painter.drawLine(screen_pts[i], screen_pts[i + 1])

    def _draw_marker(self, painter: QPainter, center: QPointF, shape: str, size: int, fill: QColor, outline: QColor, width: int):
        painter.setPen(QPen(outline, width))
        painter.setBrush(QBrush(fill))
        x, y, s = center.x(), center.y(), size
        if shape == "square":
            painter.drawRect(int(x - s), int(y - s), int(2 * s), int(2 * s))
        elif shape == "triangle":
            painter.drawPolygon(QPolygonF([QPointF(x, y - s), QPointF(x - s, y + s), QPointF(x + s, y + s)]))
        elif shape == "diamond":
            painter.drawPolygon(QPolygonF([QPointF(x, y - s), QPointF(x - s, y), QPointF(x, y + s), QPointF(x + s, y)]))
        elif shape == "cross":
            painter.drawRect(int(x - s), int(y - s), int(2 * s), int(2 * s))
            painter.drawLine(QPointF(x - s, y - s), QPointF(x + s, y + s))
            painter.drawLine(QPointF(x - s, y + s), QPointF(x + s, y - s))
        elif shape == "triangle-down":
            painter.drawPolygon(QPolygonF([QPointF(x - s, y - s), QPointF(x + s, y - s), QPointF(x, y + s)]))
        elif shape == "pentagon":
            pts = []
            for i in range(5):
                ang = math.radians(-90 + i * 72)
                pts.append(QPointF(x + s * math.cos(ang), y + s * math.sin(ang)))
            painter.drawPolygon(QPolygonF(pts))
        else:
            painter.drawEllipse(center, s, s)

    def paintEvent(self, event):
        self.competitor_hitboxes = []
        self.recommendation_hitboxes = []
        self.recommendation_card_apply_hitbox = None
        self.recommendation_card_row_id = None
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fafafa"))
        rect = self._plot_rect()
        painter.setPen(QPen(QColor("#dddddd"), 1))
        painter.drawRect(rect)

        if not self.points and not self.competitors:
            painter.setPen(QColor("#666666"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Load data to start editing.")
            return

        x0, x1, y0, y1 = self._ranges()
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)

        painter.setPen(QColor("#222222"))
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(self.rect(), Qt.AlignTop | Qt.AlignHCenter, self.title)

        # RESTORE NORMAL FONT HERE
        font = QFont("Segoe UI", 9)
        font.setBold(False)
        painter.setFont(font)

        # --- Fixed € grid ---
        step = 10

        # --- Fixed € grid ---
        step = 10  # change to 5 or 20 if needed

        start = 0
        end = int(math.ceil(y1 / step) * step)

        for y_val in range(start, end + step, step):
            p = self._to_screen(x0, y_val)

            painter.setPen(QPen(QColor("#ececec"), 1))
            painter.drawLine(rect.left(), p.y(), rect.right(), p.y())

            painter.setPen(QColor("#666666"))
            painter.drawText(8, int(p.y()) + 4, f"{y_val}")

        unique_days = sorted({int(c["x"]) for c in self.competitors} | {int(p["x"]) for p in self.points})
        for day in unique_days:
            p = self._to_screen(float(day), y0)
            painter.setPen(QPen(QColor("#ececec"), 1))
            painter.drawLine(p.x(), rect.top(), p.x(), rect.bottom())
            painter.setPen(QColor("#666666"))
            painter.drawText(int(p.x()) - 8, self.height() - 18, str(day))

        if self.show_competitors:
            for item in self.competitors:
                pt = self._to_screen(item["x"], item["y"])
                self.competitor_hitboxes.append((pt.x(), pt.y(), item))
                outline = QColor("#198754") if self._is_unlimited(item.get("plan", "")) else QColor("#bdbdbd")
                width = 1 if self._is_unlimited(item.get("plan", "")) else 1
                self._draw_marker(
                    painter,
                    pt,
                    self._provider_shape(item["provider"]),
                    8,
                    self._gb_fill(item.get("gb")),
                    outline,
                    width,
                )

                painter.setPen(QColor("#333333"))

                if self.show_prices:
                    label = f'{float(item.get("y", 0.0)):.2f}'
                else:
                    label = (
                        str(int(item["gb"]))
                        if item.get("gb") is not None and float(item["gb"]).is_integer()
                        else str(item.get("gb", ""))
                    )

                painter.drawText(int(pt.x()) - 14, int(pt.y()) - 11, label)

        selected_plan = None
        for p in self.points:
            if str(p.get("row_id")) == str(self.selected_row_id):
                selected_plan = str(p.get("plan"))
                break

        highlight_blue = QColor("#1565c0")

        plans = sorted({str(p["plan"]) for p in self.points})
        for plan in plans:
            package_points = [p for p in self.points if str(p["plan"]) == plan]
            package_points.sort(key=lambda p: (p["x"], p["gb"] if p["gb"] is not None else -1))

            if selected_plan == str(plan):
                max_rev = max([float(p.get("last_month_revenue", 0.0)) for p in package_points] + [0.0])

                if max_rev > 0:
                    bar_max_height = rect.height() * 0.12
                    bar_width = max(8, int(rect.width() / max(len(package_points) * 4, 1)))

                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QBrush(QColor(220, 220, 220, 120)))

                    for p in package_points:
                        revenue = float(p.get("last_month_revenue", 0.0))
                        if revenue <= 0:
                            continue

                        x_pos = self._to_screen(p["x"], 0).x()
                        bar_h = bar_max_height * revenue / max_rev

                        painter.drawRect(
                            int(x_pos - bar_width / 2),
                            int(rect.bottom() - bar_h),
                            int(bar_width),
                            int(bar_h),
                        )

            line_color = QColor("#198754") if self._is_unlimited(plan) else QColor("#bdbdbd")
            line_width = 1 if self._is_unlimited(plan) else 1
            is_selected_plan = selected_plan == str(plan)

            if is_selected_plan:
                cost_floor_pts = [
                    {"x": p["x"], "y": p["cost_floor"]}
                    for p in package_points
                    if p.get("cost_floor") is not None
                ]
                self._draw_polyline(painter, cost_floor_pts, QColor("#c62828"), 2, dashed=True)

            if is_selected_plan:
                self._draw_polyline(painter, package_points, highlight_blue, 2, dashed=False)
            else:
                self._draw_polyline(painter, package_points, line_color, line_width, dashed=False)

            for point in package_points:
                pt = self._to_screen(point["x"], point["y"])
                is_selected = str(point["row_id"]) == str(self.selected_row_id)
                fill = QColor("#ff9800") if point.get("promo") else self._gb_fill(point.get("gb"))
                outline = QColor("#198754") if self._is_unlimited(plan) else QColor("#bdbdbd")
                width = 1 if self._is_unlimited(plan) else 1
                marker_size = 9 if is_selected else 8
                if is_selected and not self._is_unlimited(plan):
                    outline = QColor("#555555")
                floor_state = self._floor_marker_state(point)

                # Cost-floor visualization is factual only; AllowBelowCost affects
                # export eligibility, not chart markers. Active/both-below points
                # are X-only so there is no ambiguous circle underneath.
                if floor_state in {"active_below", "both_below"}:
                    cross_color = QColor("#1565c0") if floor_state == "active_below" else QColor("#c62828")
                    cross_half = 4
                    painter.setPen(QPen(cross_color, 2))
                    painter.drawLine(
                        QPointF(pt.x() - cross_half, pt.y() - cross_half),
                        QPointF(pt.x() + cross_half, pt.y() + cross_half),
                    )
                    painter.drawLine(
                        QPointF(pt.x() - cross_half, pt.y() + cross_half),
                        QPointF(pt.x() + cross_half, pt.y() - cross_half),
                    )
                else:
                    if point.get("is_new_entry"):
                        self._draw_marker(painter, pt, "circle", marker_size, fill, QColor("#00897b"), 3)
                    else:
                        self._draw_marker(painter, pt, "circle", marker_size, fill, outline, width)

                    if floor_state == "other_below":
                        # Other currency only: blue ring around the normal point.
                        painter.setPen(QPen(QColor("#1565c0"), 2))
                        painter.setBrush(Qt.NoBrush)
                        painter.drawEllipse(pt, marker_size + 2, marker_size + 2)

                painter.setPen(QColor("#333333"))

                if self.show_prices:
                    label = f'{float(point.get("y", 0.0)):.2f}'
                else:
                    label = str(int(point["gb"])) if point.get("gb") is not None and float(point["gb"]).is_integer() else str(point.get("gb", ""))
                
                painter.drawText(int(pt.x()) - 14, int(pt.y()) - 11, label)

                recommendation = point.get("recommendation")
                if recommendation and recommendation.get("display_actionable"):
                    direction = str(recommendation.get("Direction", "")).upper()
                    if direction in {"UP", "DOWN"}:
                        hitbox = self._draw_recommendation_arrow(
                            painter,
                            pt,
                            direction,
                            str(recommendation.get("Confidence", "")),
                            str(recommendation.get("Mechanism", "")),
                        )
                        self.recommendation_hitboxes.append(
                            (hitbox, str(point.get("row_id", "")), recommendation)
                        )
                
        for m in self.promo_markers:
            pt = self._to_screen(m["x"], m["y"])

            if m.get("is_remove"):
                # Draw red X
                painter.setPen(QPen(QColor("#c62828"), 2))
                painter.drawLine(pt.x() - 5, pt.y() - 5, pt.x() + 5, pt.y() + 5)
                painter.drawLine(pt.x() - 5, pt.y() + 5, pt.x() + 5, pt.y() - 5)
            else:
                self._draw_marker(painter, pt, "diamond", 6, QColor("#ffd7a1"), QColor("#ef6c00"), 1)
                painter.setPen(QColor("#7a4b00"))

                label = f'{m.get("promo_code", "")} → {m.get("y", 0):.2f}'
                painter.drawText(int(pt.x()) + 9, int(pt.y()) + 4, label)

        legend_x = rect.left() + 10
        y = rect.top() + 10
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x), int(y), "Legend")
        y += 14

        painter.setPen(QPen(QColor("#bdbdbd"), 1))
        painter.drawLine(int(legend_x), int(y), int(legend_x) + 20, int(y))
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "HT / competition")
        y += 14

        painter.setPen(QPen(QColor("#198754"), 1))
        painter.drawLine(int(legend_x), int(y), int(legend_x) + 20, int(y))
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Unlimited")
        y += 14

        up_center = QPointF(legend_x + 10, y)
        self._draw_recommendation_arrow(
            painter, QPointF(up_center.x() - 20, up_center.y()), "UP", "HIGH", "LIST_PRICE"
        )
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 38, int(y) + 4, "L = list-price recommendation")
        y += 18

        down_center = QPointF(legend_x + 10, y)
        self._draw_recommendation_arrow(
            painter, QPointF(down_center.x() - 20, down_center.y()), "DOWN", "HIGH", "PROMO"
        )
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 38, int(y) + 4, "P = promo / net-price recommendation")
        y += 18

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, QColor("#ff9800"), QColor("#ef6c00"), 1)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Applied promo")
        y += 22

        painter.setPen(QPen(QColor("#1565c0"), 2))
        painter.drawLine(legend_x + 6, y - 4, legend_x + 14, y + 4)
        painter.drawLine(legend_x + 6, y + 4, legend_x + 14, y - 4)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Active currency below floor")
        y += 22

        painter.setPen(QPen(QColor("#c62828"), 2))
        painter.drawLine(legend_x + 6, y - 4, legend_x + 14, y + 4)
        painter.drawLine(legend_x + 6, y + 4, legend_x + 14, y - 4)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Both currencies below floor")
        y += 22

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, self._gb_fill(10), QColor("#1565c0"), 3)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Other currency below floor")
        y += 22

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, self._gb_fill(10), QColor("#00897b"), 3)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "New entry")
        y += 22

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, self._gb_fill(10), QColor("#bdbdbd"), 1)
        self._draw_marker(painter, QPointF(legend_x + 34, y), "circle", 8, self._gb_fill(80), QColor("#bdbdbd"), 1)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 52, int(y) + 4, "More GB")
        y += 24

        legend_order = ["Vodafone", "Saily", "Holafly", "Orange"]
        shown = set()
        for provider in legend_order:
            shape = self._provider_shape(provider)
            self._draw_marker(painter, QPointF(legend_x + 10, y), shape, 7, QColor("#f4f4f4"), QColor("#999999"), 1)
            painter.setPen(QColor("#333333"))
            painter.drawText(int(legend_x) + 28, int(y) + 4, provider)
            y += 14
            shown.add(provider.lower())
        providers = sorted({str(c.get("provider", "")).strip() or "Market" for c in self.competitors})
        for provider in providers[:8]:
            if provider.strip().lower() in shown:
                continue
            shape = self._provider_shape(provider)
            self._draw_marker(painter, QPointF(legend_x + 10, y), shape, 7, QColor("#f4f4f4"), QColor("#999999"), 1)
            painter.setPen(QColor("#333333"))
            painter.drawText(int(legend_x) + 28, int(y) + 4, provider)
            y += 14

        # --- Persistent selected-anchor recommendation card ---
        y += 8
        card_bottom = self._draw_recommendation_card(painter, rect, legend_x, y)
        if card_bottom > y:
            y = card_bottom + 8
            
        # --- Keyboard hint ---
        painter.setPen(QColor("#999999"))
        painter.setFont(QFont("", 8))

        mode = "PRICE" if self.show_prices else "GB"
        competitors = "ON" if self.show_competitors else "OFF"

        painter.drawText(
            int(rect.left() + 10),
            int(y + 10),
            f"Q -> Price/GB | S -> Competitors | H -> Home | Ctrl+E -> Currency | Mode: {mode} | Competitors: {competitors}"
        )
        
        if self.zoom_rect:
            p1, p2 = self.zoom_rect
            painter.setPen(QPen(QColor("#8888ff"), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(p1, p2))
