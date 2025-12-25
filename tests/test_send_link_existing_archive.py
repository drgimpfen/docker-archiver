import sys, os
import tempfile
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.main import app


def test_send_link_creates_token_for_existing_archive(monkeypatch):
    # Create a temporary file to act as an existing archive
    fd, path = tempfile.mkstemp(prefix='archive_', suffix='.tar.zst')
    os.close(fd)
    try:
        # Dummy DB connection/cursor
        executed = []

        class DummyCursor:
            def __init__(self):
                self._last_query = ''
            def execute(self, q, params=None):
                self._last_query = q
                executed.append((q, params))
            def fetchone(self):
                # If selecting by archive_path, return a row indicating file exists and not packing
                if 'WHERE archive_path = %s' in self._last_query:
                    return {'token': 'existing-token', 'file_path': path, 'is_packing': False, 'stack_name': 'test-stack'}
                return None
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
        monkeypatch.setattr('app.routes.api.downloads.get_db', lambda: DummyConn())

        # Capture send calls
        sent = {'calls': []}
        def fake_send(self, title, body, recipients=None, **kwargs):
            sent['calls'].append({'title': title, 'body': body, 'recipients': recipients})
            from app.notifications.adapters.base import AdapterResult
            return AdapterResult(channel='smtp', success=True)
        monkeypatch.setattr('app.notifications.adapters.smtp.SMTPAdapter.send', fake_send)

        # Ensure base_url is predictable
        monkeypatch.setattr('app.notifications.get_setting', lambda k, d=None: 'http://testserver' if k == 'base_url' else d)

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['username'] = 'test'
            resp = client.post('/api/downloads/send_link', json={'archive_path': path, 'email': 'user@example.com'})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get('token')
            assert data.get('download_url')
            assert sent['calls'], 'Expected an email to have been sent'
            assert 'user@example.com' in (sent['calls'][0]['recipients'] or [])
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
