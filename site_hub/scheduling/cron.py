from __future__ import annotations

from datetime import datetime


class CronError(ValueError): pass


def _matches(value: int, expression: str, minimum: int, maximum: int) -> bool:
    for part in expression.split(","):
        part = part.strip()
        if not part: raise CronError("empty cron component")
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            try: step = int(step_text)
            except ValueError as exc: raise CronError("invalid cron step") from exc
            if step < 1: raise CronError("cron step must be positive")
        else:
            base = part
        if base == "*": start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            try: start, end = int(left), int(right)
            except ValueError as exc: raise CronError("invalid cron range") from exc
        else:
            try: start = end = int(base)
            except ValueError as exc: raise CronError("invalid cron value") from exc
        if start < minimum or end > maximum or start > end:
            raise CronError("cron value outside allowed range")
        if start <= value <= end and (value - start) % step == 0: return True
    return False


def validate_cron(expression: str) -> str:
    fields = expression.split()
    if len(fields) != 5: raise CronError("cron must have five fields")
    checks = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    for field, (minimum, maximum) in zip(fields, checks, strict=True):
        _matches(minimum, field, minimum, maximum)
    return " ".join(fields)


def cron_matches(expression: str, value: datetime) -> bool:
    minute, hour, day, month, weekday = expression.split()
    # Python Monday=0; cron Sunday=0.
    cron_weekday = (value.weekday() + 1) % 7
    return all((
        _matches(value.minute, minute, 0, 59),
        _matches(value.hour, hour, 0, 23),
        _matches(value.day, day, 1, 31),
        _matches(value.month, month, 1, 12),
        _matches(cron_weekday, weekday, 0, 6),
    ))
