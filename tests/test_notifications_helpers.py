import pytest
from app.notifications import helpers


def test_get_setting_returns_default_when_db_fails(monkeypatch):
    class DummyConn:
        def cursor(self):
            raise Exception('db error')
    def fake_get_db():
        class Ctx:
            def __enter__(self):
                return DummyConn()
            def __exit__(self, *a):
                return False
        return Ctx()

    monkeypatch.setattr('app.notifications.helpers.get_db', fake_get_db)
    assert helpers.get_setting('nonexistent', 'fallback') == 'fallback'


def test_get_user_emails(monkeypatch):
    class DummyCur:
        def fetchall(self):
            return [{'email': 'a@example.com'}, {'email': 'b@example.com'}]
    class DummyConn:
        def cursor(self):
            return DummyCur()
    def fake_get_db():
        class Ctx:
            def __enter__(self):
                return DummyConn()
            def __exit__(self, *a):
                return False
        return Ctx()

    monkeypatch.setattr('app.notifications.helpers.get_db', fake_get_db)
    emails = helpers.get_user_emails()
    assert emails == ['a@example.com', 'b@example.com']


def test_get_subject_with_tag(monkeypatch):
    monkeypatch.setattr('app.notifications.helpers.get_setting', lambda k, d='': '[TAG]')
    assert helpers.get_subject_with_tag('Hi') == '[TAG] Hi'


def test_should_notify_true_false(monkeypatch):
    monkeypatch.setattr('app.notifications.helpers.get_setting', lambda k, d='': 'true' if k.endswith('success') else 'false')
    assert helpers.should_notify('success') is True
    assert helpers.should_notify('error') is False
