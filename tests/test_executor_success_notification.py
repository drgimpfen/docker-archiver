import pytest
from app.executor import ArchiveExecutor
from app import utils


def test_send_notification_called_on_success(monkeypatch):
    calls = []

    def fake_send(cfg, job_id, stack_metrics, duration, total_size):
        calls.append((cfg, job_id, stack_metrics, duration, total_size))

    monkeypatch.setattr('app.executor.send_archive_notification', fake_send)

    executor = ArchiveExecutor({'name': 'SuccessNotifyTest', 'stacks': []})
    executor.job_id = 11111

    # Prepare dummy stack metrics
    stack_metrics = []

    # Call the wrapper
    executor._send_notification(stack_metrics, duration=1, total_size=0)

    assert len(calls) == 1
    cfg, jid, metrics, duration, total = calls[0]
    assert cfg['name'] == 'SuccessNotifyTest'
    assert jid == 11111
    assert metrics == stack_metrics
