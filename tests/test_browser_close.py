from unittest.mock import Mock

import pytest

from jev_ultrafast import browser


def test_close_already_closed_tab(monkeypatch):
    close = Mock(side_effect=RuntimeError("{'code': -32602, 'message': 'No target with given id found'}"))
    monkeypatch.setattr(browser, 'cdp', close)
    instance = browser.Browser.__new__(browser.Browser)
    instance.target = 'gone-tab'
    instance.close()
    instance.close()
    assert instance.target is None
    assert close.call_count == 1


def test_close_preserves_other_errors(monkeypatch):
    monkeypatch.setattr(browser, 'cdp', Mock(side_effect=RuntimeError('Connection lost')))
    instance = browser.Browser.__new__(browser.Browser)
    instance.target = 'tab'
    with pytest.raises(RuntimeError, match='Connection lost'):
        instance.close()
