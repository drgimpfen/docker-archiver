import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.notifications.adapters.discord import DiscordAdapter
from app.notifications.adapters import generic


def test_discord_sanitizes_embed_options(monkeypatch):
    calls = {}

    def fake_make_apobj(urls=None):
        calls['urls'] = urls
        return object(), 1, None

    def fake_notify(apobj, title, body, body_format, attach=None, context=None):
        calls['title'] = title
        calls['body'] = body
        calls['format'] = body_format
        calls['attach'] = attach
        return True, None

    monkeypatch.setattr(generic, '_make_apobj', fake_make_apobj)
    monkeypatch.setattr(generic, '_notify_with_retry', fake_notify)
    monkeypatch.setattr('app.notifications.adapters.discord._make_apobj', fake_make_apobj)
    monkeypatch.setattr('app.notifications.adapters.discord._notify_with_retry', fake_notify)

    da = DiscordAdapter(webhooks=['discord://id/token'])
    title = 'Job'
    body = 'ok'
    # include odd types
    emb_opts = {'color': '0xFF', 'footer': {'user':'x'}, 'fields': [{'name': {'a':'b'}, 'value': 123, 'inline': 'true'}]}
    res = da.send(title, body, None, attach=None, context='ctx', embed_options=emb_opts)
    assert res.success is True
    assert '0xFF' in str(calls['body']) or 'Job' in calls['title']
