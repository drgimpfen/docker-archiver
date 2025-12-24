import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import types
from app.notifications.core import send_test_notification


def test_send_test_notification_includes_apprise_urls(monkeypatch):
    # Provide apprise URLs via get_setting
    urls = """
    discord://webhook/abc
    mailto://user@example.com
    mailtos://user:pass@smtp.example.com:587/?from=sender@example.com&to=user@example.com
    """
    monkeypatch.setattr('app.notifications.core.get_setting', lambda k, d='': urls)

    captured = {'calls': []}
    def fake_notify(apobj, title, body, body_format, context=None):
        captured['calls'].append({'title': title, 'body': body, 'format': body_format, 'context': context})
        return True

    monkeypatch.setattr('app.notifications.core._apprise_notify', fake_notify)

    # Should not raise
    send_test_notification()

    # Ensure at least one call was made
    assert captured['calls'], "No notify calls were made"

    # Ensure the test notification contains the human-friendly confirmation in at least one send
    assert any('notification configuration is working correctly' in c['body'].lower() for c in captured['calls'])
    # Ensure HTML is sent for email targets and MARKDOWN is used for chat targets
    import apprise
    formats = {c['format'] for c in captured['calls']}
    assert apprise.NotifyFormat.HTML in formats, "No HTML send detected for email targets"
    assert apprise.NotifyFormat.MARKDOWN in formats, "No MARKDOWN send detected for non-email targets"
