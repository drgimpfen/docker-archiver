from datetime import datetime, timezone
from app import utils


def test_format_datetime_aware_and_naive():
    # Display timezone is set via TZ env var in test env; ensure it's available
    tz = utils.get_display_timezone()

    # A timezone-aware datetime (UTC) should be converted to display timezone
    aware = datetime(2025, 12, 26, 12, 0, tzinfo=timezone.utc)
    s = utils.format_datetime(aware, "%Y-%m-%d %H:%M")
    assert isinstance(s, str) and len(s) > 0

    # A naive datetime (assumed UTC) should produce the same result as aware UTC
    naive = datetime(2025, 12, 26, 12, 0)
    s2 = utils.format_datetime(naive, "%Y-%m-%d %H:%M")
    assert s == s2
