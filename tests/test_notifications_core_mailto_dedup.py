import pytest
from app.notifications.adapters.base import AdapterResult


class FakeAdapter:
    instances = []

    def __init__(self, urls=None, webhooks=None):
        self.calls = []
        FakeAdapter.instances.append(self)

    def send(self, title, body, body_format=None, attach=None, context='', embed_options=None):
        self.calls.append({'title': title, 'body': body, 'format': body_format, 'context': context})
        return AdapterResult(channel='generic', success=True)


class FakeMailto:
    instances = []

    def __init__(self, urls=None):
        self.urls = list(urls or [])
        FakeMailto.instances.append(self)

    def send(self, title, body, body_format=None, attach=None, context=''):
        return AdapterResult(channel='mailto', success=True)


def test_mailto_not_duplicate(monkeypatch):
    # Provide apprise URLs that include mailto and a non-mail transport
    urls = """
    mailto://user@example.com
    smtp://smtp.example.com?from=sender@example.com
    """

    monkeypatch.setattr('app.notifications.core.get_setting', lambda k, d='': urls if k=='apprise_urls' else 'true')

    # Patch adapters
    monkeypatch.setattr('app.notifications.adapters.GenericAdapter', FakeAdapter)
    monkeypatch.setattr('app.notifications.adapters.MailtoAdapter', FakeMailto)

    # Call send_archive_notification minimal path
    from app.notifications.core import send_archive_notification

    archive_config = {'name': 'MyArchive'}
    stack_metrics = []
    # Should not raise
    send_archive_notification(archive_config, 999, stack_metrics, 1, 0)

    # Ensure MailtoAdapter was created and used, and GenericAdapter was NOT used for email URLs
    assert FakeMailto.instances, "MailtoAdapter not instantiated"
    # Ensure GenericAdapter was not used for email URLs (no generic instances created for email transports)
    assert not FakeAdapter.instances, "GenericAdapter should not be used for email URLs"
