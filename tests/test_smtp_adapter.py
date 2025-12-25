import os
import smtplib
from app.notifications.adapters.smtp import SMTPAdapter


class FakeSMTP:
    def __init__(self, server, port, timeout=None):
        self.server = server
        self.port = port
        self.timeout = timeout
        self.sent = []
        self.logged_in = False
        self.quit_called = False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, pwd):
        self.logged_in = (user, pwd)

    def send_message(self, msg):
        # Capture minimal info
        self.sent.append({'subject': msg['Subject'], 'from': msg['From'], 'to': msg['To'], 'body': msg.get_body(preferencelist=('html', 'plain')).get_content()})

    def quit(self):
        self.quit_called = True


def test_smtp_adapter_sends_email(monkeypatch, tmp_path):
    # Provide SMTP settings via application settings
    def fake_get_setting(k, d=''):
        mapping = {
            'smtp_server': 'smtp.example.com',
            'smtp_port': '587',
            'smtp_user': 'user',
            'smtp_password': 'pw',
            'smtp_from': 'sender@example.com',
            'smtp_use_tls': 'true'
        }
        return mapping.get(k, d)

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    fake = FakeSMTP('smtp.example.com', 587)

    def fake_smtp_factory(server, port, timeout=None):
        return fake

    monkeypatch.setattr(smtplib, 'SMTP', fake_smtp_factory)
    monkeypatch.setattr('app.notifications.get_user_emails', lambda: ['recipient@example.com'])

    adapter = SMTPAdapter()
    res = adapter.send('Subject', '<h1>Hi</h1><p>Body</p>', attach=None)
    assert res.success is True
    assert fake.sent, 'no message sent'
    sent = fake.sent[0]
    assert 'Subject' in sent['subject'] or sent['subject'] == 'Subject'
    assert 'sender@example.com' in sent['from']
    assert sent['body'].strip().startswith('<h1>Hi')


def test_smtp_adapter_with_attachment(monkeypatch, tmp_path):
    # Provide SMTP settings via application settings
    def fake_get_setting(k, d=''):
        mapping = {
            'smtp_server': 'smtp.example.com',
            'smtp_port': '587',
            'smtp_user': 'user',
            'smtp_password': 'pw',
            'smtp_from': 'sender@example.com',
            'smtp_use_tls': 'true'
        }
        return mapping.get(k, d)

    monkeypatch.setattr('app.notifications.get_setting', fake_get_setting)

    fake = FakeSMTP('smtp.example.com', 587)
    monkeypatch.setattr(smtplib, 'SMTP', lambda s, p, timeout=None: fake)
    monkeypatch.setattr('app.notifications.get_user_emails', lambda: ['recipient@example.com'])

    tf = tmp_path / 'log.txt'
    tf.write_text('log contents')

    adapter = SMTPAdapter()
    res = adapter.send('Subject', '<p>Body</p>', attach=str(tf))
    assert res.success is True
    assert fake.sent
    sent = fake.sent[0]
    assert 'Subject' in sent['subject'] or sent['subject'] == 'Subject'
