import sys, os, tempfile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.routes.api import downloads


def test_resume_generates_missing(monkeypatch, tmp_path):
    # Create a temp dir to act as archive_path
    d = tmp_path / 'stackdir'
    d.mkdir()
    archive_path = str(d)

    # Fake DB returning one candidate token (not packing, no file_path)
    queries = []
    class DummyCursor:
        def __init__(self):
            self._last_query = ''
            self._update_run = False
        def execute(self, q, params=None):
            queries.append(q)
            self._last_query = q
            # detect the atomic UPDATE and mark that it ran
            if 'UPDATE DOWNLOAD_TOKENS SET IS_PACKING' in q.strip().upper():
                self._update_run = True
        def fetchall(self):
            # first call will be for is_packing=TRUE rows (none)
            if 'is_packing = TRUE' in self._last_query:
                return []
            # candidate query
            if 'ORDER BY created_at DESC' in self._last_query:
                return [{'token': 'token-1', 'stack_name': 'stack-a', 'file_path': None, 'archive_path': archive_path, 'is_packing': False}]
            return []
        def fetchone(self):
            # If the atomic UPDATE was executed, simulate a RETURNING token
            if self._update_run:
                return {'token': 'token-1'}
            return None
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

    started = {'calls': []}

    def fake_process(stack_name, source_path, token):
        started['calls'].append((stack_name, source_path, token))

    monkeypatch.setattr('app.routes.api.downloads.process_directory_pack', fake_process)

    # Run resume with generate_missing=True — force threads to run synchronously in test
    class DummyThread:
        def __init__(self, target=None, args=(), daemon=False):
            self._target = target
            self._args = args
        def start(self):
            if self._target:
                self._target(*self._args)
    monkeypatch.setattr('threading.Thread', DummyThread)

    downloads.resume_pending_downloads(generate_missing=True)

    # Sanity: ensure the candidate query was executed
    assert any('ORDER BY created_at DESC' in q for q in queries), f"Candidate query not executed; queries={queries}"
    # Ensure the atomic UPDATE was attempted
    assert any('UPDATE DOWNLOAD_TOKENS SET IS_PACKING' in q.upper() for q in queries), f"UPDATE not attempted; queries={queries}"

    assert started['calls'], 'Expected process_directory_pack to be invoked for missing token'
    assert started['calls'][0][1] == archive_path
