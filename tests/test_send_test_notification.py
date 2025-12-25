import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.notifications import send_test_notification


def test_send_test_notification_uses_smtp(monkeypatch):
    # Provide SMTP settings via application settings
    def fake_get_setting(k, d=''):
        mapping = {
            'smtp_server': 'smtp.example.com',
            'smtp_from': 'sender@example.com'
        }
        return mapping.get(k, d)

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    captured = {'calls': []}

    # Patch SMTPAdapter.send to capture send calls
    def fake_send(self, title, body, body_format=None, attach=None, recipients=None, context=''):
        captured['calls'].append({'title': title, 'body': body, 'attach': attach, 'recipients': recipients, 'context': context})
        from app.notifications.adapters.base import AdapterResult
        return AdapterResult(channel='smtp', success=True)

    monkeypatch.setattr('app.notifications.adapters.smtp.SMTPAdapter.send', fake_send)

    # Should not raise
    send_test_notification()

    # Ensure SMTPAdapter send was called
    assert captured['calls'], 'SMTPAdapter.send was not called'
    assert 'Test Notification' in captured['calls'][0]['title']
