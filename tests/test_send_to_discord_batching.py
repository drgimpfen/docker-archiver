from app.notifications.discord_dispatch import send_to_discord


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def send(self, title, body, body_format=None, attach=None, context='', embed_options=None):
        self.calls.append({'title': title, 'body': body, 'body_format': body_format, 'attach': attach, 'context': context, 'embed_options': embed_options})
        return type('R', (), {'success': True, 'detail': None})


def test_batches_sections_to_reduce_posts():
    fa = FakeAdapter()
    title = 'T'
    body_html = '<h2>long</h2>'
    # large compact_text to cause sectioned path
    compact_text = 'X' * 5000
    # create three small sections which should be batched into fewer embeds
    sections = ['A\n1', 'B\n2', 'C\n3']

    res = send_to_discord(fa, title, body_html, compact_text, sections, attach_file=None, embed_options={'footer': 'F'}, max_desc=4000, pause=0)
    assert res['sent_any'] is True
    # We expect batching to reduce number of calls (ideally 1)
    assert len(fa.calls) <= len(sections)
    # Footer should be present in last embed
    last_embed = fa.calls[-1]
    assert 'footer' in (last_embed['embed_options'] or {})
    # And ideally we should send at most 2 posts for three small sections
    assert len(fa.calls) <= 2
