import re
from pathlib import Path

from auction.app import create_app
from auction.i18n import messages, translate


ROOT = Path(__file__).resolve().parent.parent


def test_every_static_translation_reference_exists():
    paths = [*ROOT.glob('*.py'), *ROOT.glob('*.command')]
    for folder, patterns in {
        'auction': ('*.py',), 'scripts': ('*.py', '*.sh'),
        'static': ('*.js', '*.html'), 'tests': ('*.py',),
    }.items():
        for pattern in patterns:
            paths.extend((ROOT / folder).glob(pattern))
    available = messages()
    missing = set()
    for path in paths:
        text = path.read_text(encoding='utf-8')
        keys = re.findall(r"translate\(\s*['\"]([a-z][a-z0-9_]*)['\"]", text)
        keys += re.findall(r'\bsay\s+(shell_[a-z0-9_]+)', text)
        missing.update((str(path.relative_to(ROOT)), key) for key in keys if key not in available)
    assert not missing


def test_server_renders_localized_page_and_escapes_messages(tmp_path, monkeypatch):
    app = create_app({'TESTING': True, 'DB': str(tmp_path / 'db.sqlite3'),
                      'UPLOADS': str(tmp_path / 'photos'), 'BOT_TOKEN': 'fake', 'ADMIN_ID': '1'})
    client = app.test_client()
    page = client.get('/').get_data(as_text=True)
    assert translate('page_title') in page
    assert '{{ translate' not in page
    assert client.get('/api/my-bids').json['error'] == translate('api_create_app_current_user_01')
    monkeypatch.setitem(messages(), 'page_brand', '<script>bad</script>')
    escaped = client.get('/').get_data(as_text=True)
    assert '&lt;script&gt;bad&lt;/script&gt;' in escaped
    assert '<script>bad</script>' not in escaped
