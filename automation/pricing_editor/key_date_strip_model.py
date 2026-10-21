"""Pure date geometry for the overview; no database or price projection access."""
from dataclasses import dataclass
from datetime import date, timedelta
import calendar


@dataclass(frozen=True)
class DateMarker:
    key_date: str
    effective_date: date
    kinds: tuple[str, ...]
    details: tuple[str, ...]
    readonly: bool = False

    @property
    def tooltip(self):
        state = 'Read only' if self.readonly else 'Planned changes · editable'
        return f'{self.effective_date:%d %b %Y} · {state}\n' + '\n'.join(self.details)


def build_date_markers(campaigns, key_dates, labels_by_date, readonly_keys=()):
    """Aggregate all actions on a single effective date, using the UI's key dates."""
    starts, ends = {}, {}
    for campaign in campaigns:
        if campaign.start_date:
            starts.setdefault(campaign.start_date, []).append(campaign.name)
        ends.setdefault(campaign.end_date, []).append(campaign.name)
    markers = []
    readonly = set(readonly_keys)
    for key in sorted(set(key_dates)):
        effective = date.fromisoformat(key) + timedelta(days=1)
        kinds, details = [], []
        if starts.get(effective.isoformat()):
            kinds.append('start')
            details.extend('Starts: ' + name for name in starts[effective.isoformat()])
        if ends.get(key):
            kinds.append('end')
            details.extend(f'Ends: {name} ({date.fromisoformat(key):%d %b} 24:00)' for name in ends[key])
        for label in labels_by_date.get(key, ()):
            kind = 'price' if str(label).upper().startswith('PRICE') else 'promotion'
            if kind not in kinds:
                kinds.append(kind)
            details.append(str(label))
        if not kinds:
            kinds.append('price')
        markers.append(DateMarker(key, effective, tuple(kinds), tuple(dict.fromkeys(details)), key in readonly))
    return tuple(markers)


@dataclass(frozen=True)
class DateScale:
    first: date
    last: date

    @property
    def days(self):
        return max(1, (self.last - self.first).days)

    def x(self, day, width, margin=18):
        return margin + (day - self.first).days * max(1, width - 2 * margin) / self.days

    def zoom(self, factor, fraction, maximum):
        """Zoom around the mouse position, with a useful minimum two-week view."""
        span = max(14, min(maximum, round(self.days * factor)))
        fraction = min(1., max(0., fraction))
        anchor = self.first + timedelta(days=round(self.days * fraction))
        first = anchor - timedelta(days=round(span * fraction))
        return DateScale(first, first + timedelta(days=span))

    def pan(self, days):
        shift = timedelta(days=round(days))
        return DateScale(self.first + shift, self.last + shift)

    def reveal(self, day):
        if self.first <= day <= self.last:
            return self
        first = day - timedelta(days=self.days // 2)
        return DateScale(first, first + timedelta(days=self.days))


def fit_date_scale(markers, today):
    values = [today] + [item.effective_date for item in markers]
    first, last = min(values), max(values)
    span = max(14, (last - first).days)
    padding = max(3, round(span * .04))
    return DateScale(first - timedelta(days=padding), first + timedelta(days=span + padding))


def month_ticks(scale):
    """Calendar ticks remain bounded even when a schedule spans many years."""
    month = scale.first.replace(day=1)
    if month < scale.first:
        month += timedelta(days=calendar.monthrange(month.year, month.month)[1])
    ticks = []
    while month <= scale.last:
        ticks.append(month)
        if month.year == 9999 and month.month == 12:
            break
        month += timedelta(days=calendar.monthrange(month.year, month.month)[1])
    return ticks


def marker_hits(markers, scale, x, width, radius=12):
    """Close dates remain individually selectable via the widget's chooser."""
    return tuple(item for item in markers
                 if scale.first <= item.effective_date <= scale.last
                 and abs(scale.x(item.effective_date, width) - x) <= radius)
