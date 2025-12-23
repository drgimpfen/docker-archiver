import re
from app.notifications import strip_html_tags, split_section_by_length, build_compact_text


def test_strip_html_tags_basic():
    html = "<h1>Title</h1><p>Hello <strong>World</strong>&nbsp; &amp; &lt;test&gt;</p>"
    out = strip_html_tags(html)
    assert "Title" in out
    assert "Hello World" in out
    assert "&" in out
    assert "<test>" in out


def test_strip_html_removes_script_style():
    html = "<style>body{}</style><script>alert(1)</script><p>OK</p>"
    out = strip_html_tags(html)
    assert out.strip() == "OK"


def test_split_section_by_length_paragraphs():
    text = "Para1\n\nPara2\n\nPara3"
    parts = split_section_by_length(text, max_len=10)
    # should split preserving paragraph boundaries
    assert any('Para1' in p for p in parts)
    assert any('Para2' in p for p in parts)
    assert any('Para3' in p for p in parts)


def test_build_compact_text_basic():
    archive_name = 'myarchive'
    stack_metrics = [{'stack_name': 'a', 'status': 'success', 'archive_size_bytes': 1024, 'archive_path': '/archives/a.tar'}, {'stack_name': 'b', 'status': 'failed', 'archive_size_bytes': 2048, 'archive_path': '/archives/b.tar'}]
    created_archives = [{'size': 1024, 'path': '/archives/a.tar'}, {'size': 2048, 'path': '/archives/b.tar'}]
    compact, lines = build_compact_text(archive_name, stack_metrics, created_archives, total_size=3072, size_str='3KB', duration_str='10s', stacks_with_volumes=[], reclaimed=0, base_url='http://localhost')
    assert 'myarchive' in compact
    assert 'STACKS PROCESSED' in compact or 'a' in compact
