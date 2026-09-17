import pytest

from jev_ultrafast import model


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///etc/passwd', 'https://', 'https://a b.com'])
def test_rejects_non_web_destinations(url):
    with pytest.raises(ValueError):
        model.starting_url('Find a product', url)


def test_explicit_url_does_not_call_model(monkeypatch):
    def unexpected(*args):
        raise AssertionError('No model call needed')
    monkeypatch.setattr(model, 'post_json', unexpected)
    assert model.starting_url('Find a product', 'https://example.com/') == 'https://example.com/'


def test_searches_instead_of_guessing_domain(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'test-only')
    monkeypatch.setattr(model, 'post_json', lambda *a: {
        'choices': [{'message': {'content': '{"query":"هوش مصنوعی نقطه سایت رسمی"}'}}]
    })
    url = urlsplit(model.starting_url('برو سایت هوش مصنوعی نقطه ثبت نام کن'))
    assert url.hostname == 'www.google.com'
    assert parse_qs(url.query)['q'] == ['هوش مصنوعی نقطه سایت رسمی']


def test_rejects_model_invented_destination(monkeypatch):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'test-only')
    monkeypatch.setattr(model, 'post_json', lambda *a: {
        'choices': [{'message': {'content': '{"url":"https://invented.example/"}'}}]
    })
    with pytest.raises(ValueError, match='search query'):
        model.starting_url('Find the official website')
