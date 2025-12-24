import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.notifications.adapters.discord import DiscordAdapter
from app.notifications.adapters import generic


def test_discord_adapter_embeds_with_fields_and_attachment(monkeypatch, tmp_path):
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

    # Create a temp file to attach
    tf = tmp_path / "log.txt"
    tf.write_text("log content")

    da = DiscordAdapter(webhooks=['discord://id/token'])
    title = 'Job Complete'
    body = '<h1>Result</h1>All stacks succeeded.'
    emb_opts = {'color': 0x2ECC71, 'footer': 'Job 1', 'fields': [{'name':'Test','value':'Value','inline':True}]}
    res = da.send(title, body, None, attach=str(tf), context='ctx', embed_options=emb_opts)
    assert res.success is True
    assert 'discord.com/api/webhooks' in calls['urls'][0]
    assert 'Job 1' in calls['body']
    assert 'Test' in calls['body']
    assert calls['attach'] == str(tf)
