
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
        else:
            self.setCursor(Qt.ArrowCursor)

        promo_code = self._nearest_promo_marker(event.position())
        if promo_code:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        # --- Dragging a point ---
        if self.drag_index is not None and 0 <= self.drag_index < len(self.points):
            self.setCursor(Qt.SizeVerCursor)
            y = self._from_screen_y(event.position().y())
            row_id = self.points[self.drag_index]["row_id"]
            self.pointDragged.emit(row_id, y, self.drag_index)
            return

        promo_code = self._nearest_promo_marker(event.position())
        if promo_code:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        # --- ✅ FIX: competitor hover FIRST ---
        comp = self._nearest_competitor(event.position()) if self.show_competitors else None
        if comp is not None:
            msg = (
                f'Operator: {comp["provider"]} | Plan: {display_plan_label(comp.get("plan", ""))} | '
                f'Days: {comp.get("days", "")} | GB: {comp.get("gb", "")} | '
                f'Price: {comp["y"]:.2f} | Promo: no'
            )
            self.statusChanged.emit(msg)
            #QToolTip.showText(event.globalPosition().toPoint(), msg, self)
            return

        # --- HT hover ---
        idx = self._nearest_point_index(event.position())
        if idx is not None:
            p = self.points[idx]
            msg = (
                f'Operator: HT | Plan: {display_plan_label(p["plan"])} | Days: {p["days"]} | '
                f'GB: {p["gb"]} | Price: {p["y"]:.2f} | Promo: {p["promo"] or "no"}'
            )
            self.statusChanged.emit(msg)
            # QToolTip.showText(event.globalPosition().toPoint(), msg, self)
            return

        # --- Promo hover ---
        promo_code = self._nearest_promo_marker(event.position())
        self.statusChanged.emit(f"Promo option: {promo_code}" if promo_code else "")
        
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

    def _is_partner_export_blocked(self, point: dict[str, Any]) -> bool:
        return bool(point.get("is_partner_export_blocked") or point.get("partner_export_blocked"))

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
        if other_below or self._is_partner_export_blocked(point):
            return "other_below"
        return "ok"

    def _data_ranges(self):
        xs = [p["x"] for p in self.points] + [c["x"] for c in self.competitors]

        if not xs:
            xs = [0.0, 1.0]

        x0, x1 = min(xs), max(xs)

        # Use HT data only for chart scaling
        ht_prices = [p["y"] for p in self.points] + [p["base_y"] for p in self.points]

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
            # 1) If right-click is on a promo / remove marker, apply that action
            promo_code = self._nearest_promo_marker(event.position())
            if promo_code:
                self.promoSelected.emit(promo_code)
                return

            # 2) If right-click is on a price point, select it
            idx = self._nearest_point_index(event.position())
            if idx is not None:
                row_id = self.points[idx]["row_id"]
                self.selected_row_id = row_id
                self.show_promo_markers = True
                self.pointSelected.emit(row_id)
                return

            # 3) Otherwise keep old right-click zoom behavior
            self.zoom_start = event.position()
            self.zoom_rect = None
            return

        if event.button() == Qt.LeftButton:
            idx = self._nearest_point_index(event.position())
            if idx is not None:
                self.show_promo_markers = False
                self.promo_markers = []
                self.drag_index = idx
                self.is_dragging = True

                row_id = self.points[idx]["row_id"]
                self.selected_row_id = row_id

                # Left-click should visually select / drag only.
                # It should also hide any promo markers from previous right-click selection.
                self.promo_markers = []

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
        highlight_halo = QColor("#ffffff")

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
            baseline_pts = [{"x": p["x"], "y": p.get("base_display_y", p["base_y"])} for p in package_points]
            self._draw_polyline(painter, baseline_pts, QColor("#dcdcdc"), 1, dashed=True)

            if is_selected_plan:
                cost_floor_pts = [
                    {"x": p["x"], "y": p["cost_floor"]}
                    for p in package_points
                    if p.get("cost_floor") is not None
                ]
                self._draw_polyline(painter, cost_floor_pts, QColor("#c62828"), 2, dashed=True)

            if is_selected_plan:
                self._draw_polyline(painter, package_points, highlight_halo, 8, dashed=False)
                self._draw_polyline(painter, package_points, highlight_blue, 4, dashed=False)
            else:
                self._draw_polyline(painter, package_points, line_color, line_width, dashed=False)

            for point in package_points:
                pt = self._to_screen(point["x"], point["y"])
                is_selected = str(point["row_id"]) == str(self.selected_row_id)
                fill = QColor("#ff9800") if point.get("promo") else self._gb_fill(point.get("gb"))
                outline = QColor("#198754") if self._is_unlimited(plan) else QColor("#bdbdbd")
                width = 1 if self._is_unlimited(plan) else 1
                marker_size = 9 if is_selected else 8
                if is_selected_plan:
                    self._draw_marker(painter, pt, "circle", marker_size + 3, QColor("#ffffff"), QColor("#ffffff"), 1)
                    outline = highlight_blue
                    width = 3
                    marker_size += 1 if is_selected else 0
                elif is_selected and not self._is_unlimited(plan):
                    outline = QColor("#555555")
                floor_state = self._floor_marker_state(point)
                if floor_state in {"active_below", "both_below"}:
                    if is_selected_plan:
                        self._draw_marker(painter, pt, "circle", marker_size, fill, outline, width)
                    cross_color = QColor("#1565c0") if floor_state == "active_below" else QColor("#c62828")
                    painter.setPen(QPen(cross_color, 3))
                    painter.drawLine(pt.x() - 7, pt.y() - 7, pt.x() + 7, pt.y() + 7)
                    painter.drawLine(pt.x() - 7, pt.y() + 7, pt.x() + 7, pt.y() - 7)
                elif floor_state == "other_below":
                    self._draw_marker(painter, pt, "circle", marker_size, fill, outline if is_selected_plan else QColor("#1565c0"), 3)
                elif point.get("is_new_entry"):
                    self._draw_marker(painter, pt, "circle", marker_size, fill, outline if is_selected_plan else QColor("#00897b"), 3)
                else:
                    self._draw_marker(painter, pt, "circle", marker_size, fill, outline, width)

                painter.setPen(QColor("#333333"))

                if self.show_prices:
                    label = f'{float(point.get("y", 0.0)):.2f}'
                else:
                    label = str(int(point["gb"])) if point.get("gb") is not None and float(point["gb"]).is_integer() else str(point.get("gb", ""))
                
                painter.drawText(int(pt.x()) - 14, int(pt.y()) - 11, label)
                
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

        painter.setPen(QPen(QColor("#dcdcdc"), 1, Qt.DashLine))
        painter.drawLine(int(legend_x), int(y), int(legend_x) + 20, int(y))
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Model baseline")
        y += 20

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, QColor("#ff9800"), QColor("#ef6c00"), 1)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Applied promo")
        y += 22

        painter.setPen(QPen(QColor("#1565c0"), 3))
        painter.drawLine(legend_x + 3, y - 7, legend_x + 17, y + 7)
        painter.drawLine(legend_x + 3, y + 7, legend_x + 17, y - 7)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "This currency below")
        y += 22

        painter.setPen(QPen(QColor("#c62828"), 3))
        painter.drawLine(legend_x + 3, y - 7, legend_x + 17, y + 7)
        painter.drawLine(legend_x + 3, y + 7, legend_x + 17, y - 7)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Both currencies below")
        y += 22

        self._draw_marker(painter, QPointF(legend_x + 10, y), "circle", 8, self._gb_fill(10), QColor("#1565c0"), 3)
        painter.setPen(QColor("#333333"))
        painter.drawText(int(legend_x) + 28, int(y) + 4, "Other currency below")
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
