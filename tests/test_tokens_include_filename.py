import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.main import app


def test_tokens_include_file_name(monkeypatch):
    # Fake DB returning a token row with file_path
    now = datetime.utcnow()
    file_path = '/tmp/downloads/teststack_20250101_000000.tar.zst'

    class DummyCursor:
        def execute(self, q, params=None):
            self._last_query = q
        def fetchall(self):
            return [{'token': 't1', 'stack_name': 'my-stack', 'created_at': now, 'expires_at': now + timedelta(hours=1), 'is_packing': False, 'file_path': file_path, 'notify_emails': ['a@example.com']}]
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

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'test'
        resp = client.get('/api/downloads/tokens')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tokens'][0]['file_name'] == os.path.basename(file_path)
