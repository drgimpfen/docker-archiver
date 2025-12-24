import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.main import app


def test_manage_security_post_sets_apply_permissions(monkeypatch):
    with app.test_client() as client:
        # Bypass get_current_user and get_db
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
        get_resp = client.get('/settings/security')
        html = get_resp.get_data(as_text=True)
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        csrf = m.group(1) if m else ''

        resp = client.post('/settings/security', data={
            'csrf_token': csrf,
            'apply_permissions': 'on'
        }, follow_redirects=True)

        assert resp.status_code == 200
        # Ensure we attempted to insert/update apply_permissions
        found = any(params and params[0] == 'apply_permissions' and params[1] == 'true' for _, params in executed if params)
        assert found, 'apply_permissions update not attempted'


def test_manage_cleanup_post_valid_saves_and_schedules(monkeypatch):
    with app.test_client() as client:
        # Bypass get_current_user and get_db
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

        # Monkeypatch schedule_cleanup_task to record calls
        called = {'scheduled': False}
        def dummy_schedule():
            called['scheduled'] = True
        monkeypatch.setattr('app.routes.settings.schedule_cleanup_task', dummy_schedule)

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'test'

        # Fetch page to obtain CSRF token
        get_resp = client.get('/settings/cleanup')
        html = get_resp.get_data(as_text=True)
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        csrf = m.group(1) if m else ''

        resp = client.post('/settings/cleanup', data={
            'csrf_token': csrf,
            'cleanup_enabled': 'on',
            'cleanup_cron': '30 2 * * *',
            'cleanup_log_retention_days': '60',
            'cleanup_dry_run': 'on',
            'notify_cleanup': 'on'
        }, follow_redirects=True)

        assert resp.status_code == 200
        data = resp.get_data(as_text=True)
        # Ensure we attempted to update cleanup_cron and retention days
        found_cron = any(params and params[0] == 'cleanup_cron' and params[1] == '30 2 * * *' for _, params in executed if params)
        found_retention = any(params and params[0] == 'cleanup_log_retention_days' and params[1] == '60' for _, params in executed if params)
        assert found_cron, 'cleanup_cron update not attempted'
        assert found_retention, 'cleanup_log_retention_days update not attempted'
        assert called['scheduled'], 'schedule_cleanup_task was not called'
