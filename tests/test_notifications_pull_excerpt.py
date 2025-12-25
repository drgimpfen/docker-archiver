import re
from app.notifications import send_archive_notification
from app.notifications.adapters.base import AdapterResult


def test_send_notification_includes_pull_excerpt(monkeypatch):
    # Prepare settings via monkeypatched get_setting used by notifications
    def fake_get_setting(key, default=''):
        if key == 'notify_on_success':
            return 'true'
        if key == 'smtp_server':
            return 'smtp.example.local'
        if key == 'smtp_from':
            return 'noreply@example.local'
        if key == 'image_pull_excerpt_lines':
            return '3'
        return default

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    # Prepare stack metrics with pull_output containing multiple lines and a line that needs escaping
    pull_output = """Downloaded newer image foo:latest
Some more detail about pull
<dangerous & data>
Fourth line
Fifth line
"""
    stack_metrics = [
        {
            'stack_name': 'teststack',
            'status': 'success',
            'images_pulled': True,
            'pull_output': pull_output,
        }
    ]

    captured = {}

    # Patch SMTPAdapter.send to capture the body that would be sent
    def fake_send(self, title, body, body_format=None, attach=None, recipients=None, context=''):
        captured['title'] = title
        captured['body'] = body
        return AdapterResult(channel='smtp', success=True)

    monkeypatch.setattr('app.notifications.adapters.smtp.SMTPAdapter.send', fake_send)

    # Call the notifier
    send_archive_notification({'name': 'PullExcerptTest'}, 12345, stack_metrics, duration=10, total_size=0)

    body = captured.get('body', '')
    assert body, "Expected notification body to be sent"

    # The excerpt should contain the full (filtered) pull output and should be HTML-escaped
    assert 'Downloaded newer image foo:latest' in body
    assert 'Some more detail about pull' in body
    # The third line included should be escaped
    assert '&lt;dangerous &amp; data&gt;' in body
    # The fourth and fifth lines should also be included now (full filtered output)
    assert 'Fourth line' in body
    assert 'Fifth line' in body

    # Also ensure the note references pull output
    assert 'Pull output' in body


def test_send_notification_filters_progress_lines(monkeypatch):
    def fake_get_setting(key, default=''):
        if key == 'notify_on_success':
            return 'true'
        if key == 'smtp_server':
            return 'smtp.example.local'
        if key == 'smtp_from':
            return 'noreply@example.local'
        if key == 'image_pull_excerpt_lines':
            return '0'  # 0 -> show full filtered output
        return default

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    # Simulate pull output with progress updates and final lines
    pull_output = """Pulled: fetching layer\rDownloading [===>     ] 10MB/50MB\rDownloading [======>  ] 30MB/50MB\r
Pulling fs layer\nDownloaded newer image foo:latest\nFinal step completed\n"""

    stack_metrics = [
        {
            'stack_name': 'progstack',
            'status': 'success',
            'images_pulled': True,
            'pull_output': pull_output,
        }
    ]

    captured = {}

    def fake_send(self, title, body, body_format=None, attach=None, recipients=None, context=''):
        captured['body'] = body
        return AdapterResult(channel='smtp', success=True)

    monkeypatch.setattr('app.notifications.adapters.smtp.SMTPAdapter.send', fake_send)

    send_archive_notification({'name': 'ProgTest'}, 54321, stack_metrics, duration=5, total_size=0)

    body = captured.get('body', '')
    assert body

    # Progress lines should be filtered out
    assert 'Downloading [' not in body
    assert '\r' not in body
    # Final lines should remain
    assert 'Downloaded newer image foo:latest' in body
    assert 'Final step completed' in body


def test_send_notification_preserves_final_lines_in_realistic_output(monkeypatch):
    def fake_get_setting(key, default=''):
        if key == 'notify_on_success':
            return 'true'
        if key == 'smtp_server':
            return 'smtp.example.local'
        if key == 'smtp_from':
            return 'noreply@example.local'
        if key == 'image_pull_excerpt_lines':
            return '0'  # full filtered output
        return default

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    sample = '''[+] Pulling 15/15
 ✔ db Pulled                                                         12.4s 
   ✔ 45b42c59be33 Already exists                                      0.0s 
   ⠹ 40adec129f1a Downloading  3.374MB/4.178MB                        9.3s 
   ✔ b4c431d00c78 Download complete                                   9.3s 
 ✔ web Pulled                                                         15.2s
   ✔ 2696974e2815 Download complete                                   9.3s 
   ⠹ 564b77596399 Extracting     5.622MB/7.965MB                      9.3s
'''

    stack_metrics = [
        {
            'stack_name': 'realstack',
            'status': 'success',
            'images_pulled': True,
            'pull_output': sample,
        }
    ]

    captured = {}

    def fake_send(self, title, body, body_format=None, attach=None, recipients=None, context=''):
        captured['body'] = body
        return AdapterResult(channel='smtp', success=True)

    monkeypatch.setattr('app.notifications.adapters.smtp.SMTPAdapter.send', fake_send)

    send_archive_notification({'name': 'RealTest'}, 22222, stack_metrics, duration=2, total_size=0)

    body = captured.get('body', '')
    assert body

    # Should keep summary and completed lines
    assert '[+] Pulling 15/15' in body
    assert '✔ db Pulled' in body
    assert 'Already exists' in body
    assert 'Download complete' in body
    assert '✔ web Pulled' in body

    # Should NOT include spinner/progress lines
    assert 'Downloading  3.374MB/4.178MB' not in body
    assert 'Extracting     5.622MB/7.965MB' not in body
