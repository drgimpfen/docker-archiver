import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.main import app


def test_manage_notifications_post_sets_settings(monkeypatch):
    with app.test_client() as client:
        # Bypass get_current_user and get_db to avoid DB dependency in tests
        monkeypatch.setattr('app.routes.settings.get_current_user', lambda: {'id': 1, 'username': 'test'})

        executed = []
        class DummyCursor:
            def execute(self, q, params=None):
                executed.append((q, params))
            def fetchall(self):
                return []
        class DummyConn:
            def cursor(self):
                return DummyCursor()
            def __enter__(self):
                return self
            def commit(self):
                pass
            def __exit__(self, exc_type, exc, tb):
                return False
        monkeypatch.setattr('app.routes.settings.get_db', lambda: DummyConn())


        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'test'

        # Fetch page to obtain CSRF token
        get_resp = client.get('/settings/notifications')
        html = get_resp.get_data(as_text=True)
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        csrf = m.group(1) if m else ''

        resp = client.post('/settings/notifications', data={
            'csrf_token': csrf,
            'smtp_server': 'smtp.example.com',
            'smtp_port': '2525',
            'smtp_user': 'user',
            'smtp_password': 'pw',
            'smtp_from': 'sender@example.com',
            'smtp_use_tls': 'on',
            'notification_subject_tag': '[Test]',
            'notify_success': 'on'
        }, follow_redirects=True)

        assert resp.status_code == 200
        data = resp.get_data(as_text=True)
        # Ensure we attempted to insert/update settings for smtp_server and notification_subject_tag
        found_smtp = any(params and params[0] == 'smtp_server' and params[1] == 'smtp.example.com' for _, params in executed if params)
        found_subject = any(params and params[0] == 'notification_subject_tag' and params[1] == '[Test]' for _, params in executed if params)
        assert found_smtp, 'smtp_server update not attempted'
        assert found_subject, 'notification_subject_tag update not attempted'
