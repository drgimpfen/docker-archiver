import pytest
from app.executor import ArchiveExecutor
from app import utils


def test_phase3_finalize_sends_failure_notification(monkeypatch):
    calls = []

    def fake_send(cfg, job_id, stack_metrics, duration, total_size):
        calls.append((cfg, job_id, stack_metrics, duration, total_size))

    # Patch the function in the executor module where it's referenced
    monkeypatch.setattr('app.executor.send_archive_failure_notification', fake_send)

    # Create executor and stub out DB-updating methods
    executor = ArchiveExecutor({'name': 'FailNotifyTest'})
    executor.job_id = 12345
    captured = {}
    def fake_update(status, end_time=None, duration=None, total_size=None, error=None):
        captured['status'] = status
        captured['error'] = error

    executor._update_job_status = fake_update
    executor._save_stack_metrics = lambda *args, **kwargs: None

    start_time = utils.now() - __import__('datetime').timedelta(seconds=10)
    stack_metrics = [
        {
            'stack_name': 'badstack',
            'status': 'failed',
            'archive_size_bytes': 0,
            'duration_seconds': 0,
            'start_time': start_time,
            'error': 'start failed'
        }
    ]

    # Invoke finalize
    executor._phase_3_finalize(start_time, stack_metrics)

    assert len(calls) == 1
    cfg, jid, metrics, duration, total = calls[0]
    assert cfg['name'] == 'FailNotifyTest'
    assert jid == 12345
    assert metrics == stack_metrics

    # Verify job status updated to 'failed'
    assert captured.get('status') == 'failed'
    assert captured.get('error') is not None


def test_phase3_finalize_sends_failure_notification_when_job_failed_flag_set(monkeypatch):
    calls = []

    def fake_send(cfg, job_id, stack_metrics, duration, total_size):
        calls.append((cfg, job_id, stack_metrics, duration, total_size))

    monkeypatch.setattr('app.executor.send_archive_failure_notification', fake_send)

    executor = ArchiveExecutor({'name': 'FlagFailTest'})
    executor.job_id = 22222
    executor.job_failed = True

    captured = {}
    def fake_update(status, end_time=None, duration=None, total_size=None, error=None):
        captured['status'] = status
        captured['error'] = error
    executor._update_job_status = fake_update
    executor._save_stack_metrics = lambda *args, **kwargs: None

    start_time = utils.now() - __import__('datetime').timedelta(seconds=5)
    stack_metrics = [
        {
            'stack_name': 'okstack',
            'status': 'success',
            'archive_size_bytes': 0,
            'duration_seconds': 0,
            'start_time': start_time,
            'error': None
        }
    ]

    executor._phase_3_finalize(start_time, stack_metrics)

    assert len(calls) == 1
    cfg, jid, metrics, duration, total = calls[0]
    assert cfg['name'] == 'FlagFailTest'
    assert jid == 22222
    assert metrics == stack_metrics
    assert captured.get('status') == 'failed'
    assert captured.get('error') is not None
