from app.notifications.discord_dispatch import send_to_discord


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def send(self, title, body, body_format=None, attach=None, context='', embed_options=None):
        self.calls.append({'title': title, 'body': body, 'body_format': body_format, 'attach': attach, 'context': context, 'embed_options': embed_options})
        return type('R', (), {'success': True, 'detail': None})


def test_packs_sections_into_minimum_embeds():
    fa = FakeAdapter()
    title = 'T'
    body_html = '<h2>big</h2>'
    compact_text = 'X' * 5000

    # Create three sections each ~400 chars -> with max_desc=1000 we can pack 2 sections in first embed, 1 in second
    sections = [f"S{i}\n{"x" * 400}" for i in range(1, 4)]

    res = send_to_discord(fa, title, body_html, compact_text, sections, attach_file=None, embed_options={'footer': 'F'}, max_desc=1000, pause=0)
    assert res['sent_any'] is True
    # Expect 2 embeds: 2 sections in first, 1 in final
    assert len(fa.calls) == 2
    # Footer should be present in last embed
    last_embed = fa.calls[-1]
    assert 'footer' in (last_embed['embed_options'] or {})
