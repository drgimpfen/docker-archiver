import pytest
from app.notifications.discord_dispatch import send_to_discord
from app.notifications.adapters.base import AdapterResult


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def send(self, title, body, body_format=None, attach=None, context='', embed_options=None):
        self.calls.append({'title': title, 'body': body, 'attach': attach, 'embed_options': embed_options})
        # Simulate success for all sends
        return AdapterResult(channel='discord', success=True)


def test_send_to_discord_single_short():
    fa = FakeAdapter()
    title = 'T'
    body_html = '<h2>hi</h2>'
    compact_text = 'short text'
    sections = ['short text']

    res = send_to_discord(fa, title, body_html, compact_text, sections, attach_file=None, embed_options={'footer': 'f'}, max_desc=1000, pause=0)

    assert res['sent_any'] is True
    assert len(fa.calls) == 1
    assert fa.calls[0]['attach'] is None


def test_send_to_discord_sectioned_with_attach_and_footer():
    fa = FakeAdapter()
    title = 'Archive'
    body_html = '<h2>long</h2>'
    compact_text = 'X' * 10000
    sections = ['HEADER\nSummary', 'STACKS\nstack1 X', 'FOOTER\nView details']

    res = send_to_discord(fa, title, body_html, compact_text, sections, attach_file='/tmp/x.log', embed_options={'footer': 'Job 1', 'fields': []}, max_desc=200, pause=0)

    # Should send multiple parts
    assert res['sent_any'] is True
    assert len(fa.calls) >= len(sections)
    # Last call should include the attachment
    assert fa.calls[-1]['attach'] == '/tmp/x.log'
    # Intermediate calls (excluding final embed and final attach) should not include footer in embed_options
    # The sequence is: embed parts..., final embed (may have footer), attach message
    if len(fa.calls) >= 3:
        intermediate_calls = fa.calls[:-2]
    else:
        intermediate_calls = []
    for call in intermediate_calls:
        assert 'footer' not in (call['embed_options'] or {})
    # The final embed (the last embed call) should include footer
    # Identify last embed call (it's the one before the attach call)
    if len(fa.calls) >= 2:
        last_embed_call = fa.calls[-2]
        assert 'footer' in (last_embed_call['embed_options'] or {})
        # If view_url was passed earlier, it should appear in the footer when set
        if 'View details' in last_embed_call['embed_options'].get('footer', ''):
            assert 'View details:' in last_embed_call['embed_options']['footer']
