import sys, os, tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from datetime import datetime, timedelta
from pathlib import Path
from app.main import app


def test_download_serves_file_with_string_file_path(monkeypatch, tmp_path):
    # Create a temp file to act as existing archive
    f = tmp_path / 'archive_test.tar.zst'
    f.write_text('test')
    file_path = str(f)

    now = datetime.utcnow()

    class DummyCursor:
        def execute(self, q, params=None):
            self._last_query = q
        def fetchone(self):
            return {'file_path': file_path, 'archive_path': None, 'notify_emails': None, 'stack_name': 'test', 'expires_at': now + timedelta(hours=1), 'is_packing': False}
    class DummyConn:
        def cursor(self):
            return DummyCursor()
        def __enter__(self):
            return self
        def commit(self):
            pass
        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr('app.main.get_db', lambda: DummyConn())

    # Create a dummy token that the DB will return for this token
    token = 'test-token-123'

    with app.test_client() as client:
        resp = client.get(f'/download/{token}')
        # Should return 200 OK and Content-Disposition includes filename
        assert resp.status_code == 200
        cd = resp.headers.get('Content-Disposition','')
        assert 'attachment' in cd
        assert 'archive_test' in cd
