import logging
from app.notifications.adapters.discord import DiscordAdapter


def test_detect_multiple_apprise_posts(monkeypatch, caplog):
    # Simulate _notify_with_retry returning a debug log that includes two Discord POSTs
    def fake_make_apobj(urls=None):
        return object(), 1, None

    debug_log = "Discord POST URL: https://discord.com/api/webhooks/...\nDiscord Payload: {...}\nDiscord POST URL: https://discord.com/api/webhooks/...\nDiscord Payload: {...}"

    def fake_notify(apobj, title, body, body_format=None, attach=None):
        # Embed successful but return captured logs
        return True, debug_log

    monkeypatch.setattr('app.notifications.adapters.discord._make_apobj', fake_make_apobj)
    monkeypatch.setattr('app.notifications.adapters.discord._notify_with_retry', fake_notify)

    da = DiscordAdapter(webhooks=['discord://id/token'])

    caplog.set_level(logging.WARNING)
    res = da.send('T', 'Body content', attach=None, context='test')
    assert res.success
    # We expect a warning about multiple posts to be present
    assert any('Apprise performed' in rec.message for rec in caplog.records)
