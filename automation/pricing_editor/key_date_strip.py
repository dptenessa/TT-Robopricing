"""Compact native Qt overview. Painting and navigation use cached date metadata only."""
from datetime import date
from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPolygonF
from PySide6.QtWidgets import QWidget, QSizePolicy, QToolButton, QMenu, QToolTip
from .key_date_strip_model import DateScale, fit_date_scale, month_ticks, marker_hits


class KeyDateStrip(QWidget):
    dateActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(62)
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName('Key-date timeline overview')
        self._markers = ()
        self._today = date.today()
        self._selected = ''
        self._fit = fit_date_scale((), self._today)
        self._scale = self._fit
        self._press = None
        self._dragged = False
        self._press_scale = None
        self.fit_button = QToolButton(self)
        self.fit_button.setText('Fit')
        self.fit_button.setToolTip('Show today and all scheduled dates')
        self.fit_button.setFixedSize(30, 18)
        self.fit_button.clicked.connect(self.reset_view)

    def set_markers(self, markers, *, today=None):
        markers = tuple(markers)
        today = today or date.today()
        changed_dates = (tuple(m.key_date for m in markers) != tuple(m.key_date for m in self._markers)
                         or today != self._today)
        self._markers, self._today = markers, today
        self._fit = fit_date_scale(markers, today)
        if changed_dates:
            self._scale = self._fit
        self.update()

    def set_selected_key(self, key):
        self._selected = str(key or '')
        selected = next((m.effective_date for m in self._markers if m.key_date == self._selected), self._today)
        self._scale = self._scale.reveal(selected)
        self.update()

    def reset_view(self, _checked=False):
        self._scale = self._fit
        self.update()

    def resizeEvent(self, event):
        if hasattr(self, 'fit_button'):
            self.fit_button.move(max(0, self.width() - 36), 1)
        super().resizeEvent(event)

    def _today_hit(self, point):
        x = self._scale.x(self._today, self.width())
        return self._scale.first <= self._today <= self._scale.last and abs(point.x() - x) <= 22 and 13 <= point.y() <= 26

    def _hits(self, point):
        if not 26 <= point.y() <= 61:
            return ()
        return marker_hits(self._markers, self._scale, point.x(), self.width(), radius=24 if point.y() >= 43 else 12)

    def event(self, event):
        if event.type() == QEvent.ToolTip:
            point = event.pos()
            hits = self._hits(point)
            if hits:
                QToolTip.showText(event.globalPos(), '\n\n'.join(m.tooltip for m in hits), self)
            elif self._today_hit(point):
                QToolTip.showText(event.globalPos(), 'Today · click to view current operational prices (read only)', self)
            else:
                QToolTip.showText(event.globalPos(), 'Click a marker to select a date. Wheel to zoom; drag empty space to pan. Fit shows all dates.', self)
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.position()
            self._press_scale = self._scale
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.LeftButton:
            delta = event.position().x() - self._press.x()
            if abs(delta) > 5:
                self._dragged = True
                self.setCursor(Qt.ClosedHandCursor)
                self._scale = self._press_scale.pan(-delta * self._press_scale.days / max(1, self.width() - 36))
                self.update()
            return
        self.setCursor(Qt.PointingHandCursor if self._hits(event.position()) or self._today_hit(event.position()) else Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._press is None:
            return super().mouseReleaseEvent(event)
        self._press = None
        self.setCursor(Qt.OpenHandCursor)
        if self._dragged:
            return
        hits = self._hits(event.position())
        if len(hits) == 1:
            self.dateActivated.emit(hits[0].key_date)
        elif hits:
            menu = QMenu(self)
            for marker in hits:
                name = ' · '.join(marker.details) or 'Price change'
                action = menu.addAction(f'{marker.effective_date:%d %b %Y} · {name}')
                font = action.font()
                font.setItalic(marker.readonly)
                font.setBold(marker.key_date == self._selected)
                action.setFont(font)
                action.setToolTip(marker.tooltip)
                action.triggered.connect(lambda _checked=False, key=marker.key_date: self.dateActivated.emit(key))
            menu.exec(event.globalPosition().toPoint())
        elif self._today_hit(event.position()):
            self.dateActivated.emit('')

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return super().wheelEvent(event)
        fraction = (event.position().x() - 18) / max(1, self.width() - 36)
        self._scale = self._scale.zoom(.75 if delta > 0 else 1.33, fraction, max(30, self._fit.days * 2))
        self.update()
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('#F5F6F9'))
        painter.setPen(QColor('#D6DAE1'))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        legend = QFont('Segoe UI', 7)
        painter.setFont(legend)
        painter.setPen(QColor('#59616E'))
        painter.drawText(QRectF(10, 0, max(1, self.width() - 54), 14), Qt.AlignLeft | Qt.AlignVCenter,
                         '◆ Prices   ▲ Start   ▼ End   ○ Promotion   ·   italic: read only   ·   wheel: zoom / drag: pan')
        axis = 34
        painter.setPen(QPen(QColor('#B7BFCA'), 1))
        painter.drawLine(18, axis, self.width() - 18, axis)
        today_x = self._scale.x(self._today, self.width())
        month_right = -100.
        for month in month_ticks(self._scale):
            x = self._scale.x(month, self.width())
            painter.setPen(QColor('#BBC3CE'))
            painter.drawLine(QPointF(x, 29), QPointF(x, 39))
            if x > month_right + 8 and abs(x - today_x) > 50:
                text = month.strftime('%b %Y')
                painter.setPen(QColor('#6D7581'))
                painter.drawText(QRectF(x - 12, 14, 65, 15), Qt.AlignLeft | Qt.AlignVCenter, text)
                month_right = x + 48
        if self._scale.first <= self._today <= self._scale.last:
            painter.setPen(QPen(QColor('#277C7A'), 1.5))
            painter.drawLine(QPointF(today_x, 17), QPointF(today_x, 43))
            painter.setPen(QColor('#277C7A'))
            today_font = QFont('Segoe UI', 7)
            today_font.setBold(not self._selected)
            painter.setFont(today_font)
            painter.drawText(QRectF(max(4, min(today_x - 19, self.width() - 42)), 14, 42, 15), Qt.AlignCenter, 'Today')
        visible = [m for m in self._markers if self._scale.first <= m.effective_date <= self._scale.last]
        selected = next((m for m in visible if m.key_date == self._selected), None)
        label_boxes = []
        # Reserve the selected label first; close-date labels never cover it.
        for marker in sorted(visible, key=lambda m: (m != selected, m.effective_date)):
            x = self._scale.x(marker.effective_date, self.width())
            active = marker.key_date == self._selected
            font = QFont('Segoe UI', 8)
            font.setItalic(marker.readonly)
            font.setBold(active)
            painter.setFont(font)
            rect = QRectF(max(2, min(x - 27, self.width() - 56)), 43, 54, 17)
            if not any(rect.intersects(other) for other in label_boxes):
                painter.setPen(QColor('#AF005C' if active else '#48515E'))
                painter.drawText(rect, Qt.AlignCenter, marker.effective_date.strftime('%d %b'))
                label_boxes.append(rect)
            if active:
                painter.setPen(QPen(QColor('#E20074'), 1.5))
                painter.setBrush(QColor('#FBE5F0'))
                painter.drawRoundedRect(QRectF(x - 13, 25, 26, 18), 5, 5)
            for index, kind in enumerate(marker.kinds):
                offset = (index - (len(marker.kinds) - 1) / 2) * 8
                point = QPointF(x + offset, axis)
                color = QColor({'price':'#D6006D','start':'#21845E','end':'#BD701C','promotion':'#626AA1'}[kind])
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(QColor('#F5F6F9') if marker.readonly else color)
                if kind == 'start':
                    painter.drawPolygon(QPolygonF([point + QPointF(0,-4),point + QPointF(-4,3),point + QPointF(4,3)]))
                elif kind == 'end':
                    painter.drawPolygon(QPolygonF([point + QPointF(0,4),point + QPointF(-4,-3),point + QPointF(4,-3)]))
                elif kind == 'price':
                    painter.drawPolygon(QPolygonF([point + QPointF(0,-4),point + QPointF(-4,0),point + QPointF(0,4),point + QPointF(4,0)]))
                else:
                    painter.drawEllipse(point, 3.5, 3.5)
        if not self._markers:
            painter.setFont(QFont('Segoe UI', 8))
            painter.setPen(QColor('#69717D'))
            painter.drawText(QRectF(60, 42, max(1,self.width()-80), 17), Qt.AlignCenter, 'No scheduled changes · add a dated change above')
