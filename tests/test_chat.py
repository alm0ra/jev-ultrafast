import threading
from unittest.mock import Mock

from jev_ultrafast import demo


def test_chat_reply_does_not_open_browser(monkeypatch):
    monkeypatch.setattr(demo, 'MESSAGES', [])
    monkeypatch.setattr(demo, 'AGENT', None)
    monkeypatch.setattr(demo, 'chat_plan', lambda *a: {
        'action': 'reply', 'goal': '', 'message': 'هنوز نتیجه‌ای پیدا نشده.'})
    constructor = Mock(side_effect=AssertionError('Must not open browser'))
    monkeypatch.setattr(demo, 'Agent', constructor)
    result = demo.command('chat', {'message': 'چی پیدا کردی؟'})
    assert result['start_run'] is False
    assert [m['role'] for m in result['messages']] == ['user', 'assistant']
    constructor.assert_not_called()


def test_blocked_summary_is_generated_once(monkeypatch):
    agent = Mock()
    agent.state = {'status': 'blocked'}
    agent.snapshot.return_value = {
        'status': 'blocked', 'page': {'text': 'No results'}, 'recovery_goals': ['one', 'two']}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    explain = Mock(return_value='جست‌وجو به نتیجه نرسید.')
    monkeypatch.setattr(demo, 'explain_result', explain)
    worker = demo.finish_message()
    worker.join(timeout=2)
    demo.command('summary', {})
    assert explain.call_count == 1
    assert len(demo.MESSAGES) == 1


def test_summary_failure_preserves_browser_state(monkeypatch):
    agent = Mock()
    agent.state = {'status': 'blocked'}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    agent.snapshot.return_value = {'status': 'blocked', 'recovery_goals': ['one', 'two']}
    monkeypatch.setattr(demo, 'explain_result', Mock(side_effect=RuntimeError('Unavailable')))
    worker = demo.finish_message()
    worker.join(timeout=2)
    assert agent.state['status'] == 'blocked'
    assert 'ناموفق' in demo.MESSAGES[0]['content']


def test_browser_request_starts_execution(monkeypatch):
    monkeypatch.setattr(demo, 'AGENT', None)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    monkeypatch.setattr(demo, 'chat_plan', lambda *a: {
        'action': 'browse', 'goal': 'Find the site and begin registration.',
        'message': 'سایت را پیدا می‌کنم.', 'query': 'official site'})
    from jev_ultrafast import model
    monkeypatch.setattr(model, 'post_json', Mock(side_effect=AssertionError('No second LLM call')))
    agent = Mock()
    agent.state = {}
    agent.snapshot.return_value = {'status': 'ready', 'page': {}}
    constructor = Mock(return_value=agent)
    monkeypatch.setattr(demo, 'Agent', constructor)
    result = demo.command('chat', {'message': 'برو ثبت نام کن'})
    assert result['start_run'] is True
    constructor.assert_called_once()
    assert result['messages'][-1]['content'] == 'سایت را پیدا می‌کنم.'


def test_slow_llm_does_not_lock_browser_or_read_live_state(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    agent = Mock()
    agent.state = {'status': 'blocked'}
    snapshot = {'status': 'blocked', 'page': {'text': 'original'}, 'recovery_goals': ['one', 'two']}
    agent.snapshot.return_value = snapshot
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])

    def slow_explanation(saved, error):
        entered.set()
        assert release.wait(timeout=2)
        assert saved['page']['text'] == 'original'
        return 'finished'

    monkeypatch.setattr(demo, 'explain_result', slow_explanation)
    worker = demo.finish_message()
    try:
        assert entered.wait(timeout=1)
        assert demo.LOCK.acquire(blocking=False)
        demo.LOCK.release()
        assert demo.MESSAGES[0]['pending'] is True
        snapshot['page']['text'] = 'changed'
        assert demo.finish_message() is None
    finally:
        release.set()
        worker.join(timeout=2)
    assert demo.MESSAGES[0]['content'].endswith('finished')
    assert demo.MESSAGES[0]['pending'] is False


def test_followup_continues_existing_tab_without_search(monkeypatch):
    agent = Mock()
    agent.state = {'page': {'url': 'https://example.test/signup', 'text': 'Phone number'}}
    agent.snapshot.return_value = {'status': 'ready', 'page': agent.state['page']}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    monkeypatch.setattr(demo, 'chat_plan', lambda *a: {
        'action': 'continue', 'goal': 'Fill the phone provided by the user.', 'message': 'ادامه می‌دهم.'})
    search = Mock(side_effect=AssertionError('Do not search on follow-up'))
    create = Mock(side_effect=AssertionError('Do not replace the tab'))
    monkeypatch.setattr(demo, 'starting_url', search)
    monkeypatch.setattr(demo, 'Agent', create)
    result = demo.command('chat', {'message': 'شماره را وارد کن'})
    assert result['start_run'] is True
    agent.continue_goal.assert_called_once_with('Fill the phone provided by the user.')
    agent.close.assert_not_called()
    search.assert_not_called()
    create.assert_not_called()


def test_stop_review_resumes_same_agent_without_browser_action(monkeypatch):
    agent = Mock()
    agent.state = {'status': 'blocked', 'goal': 'Inspect credit packages'}
    agent.snapshot.return_value = {**agent.state, 'page': {'text': 'Credits tab'}}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    monkeypatch.setattr(demo, 'review_stop', lambda s: {
        'action': 'continue', 'next_goal': 'Open the observed Credits tab', 'message': 'ادامه می‌دهم.'})
    worker = demo.finish_message()
    worker.join(timeout=2)
    assert agent.state['status'] == 'ready'
    assert agent.state['goal'] == 'Inspect credit packages'
    assert 'Credits tab' in agent.state['working_goal']
    agent.command.assert_not_called()
    assert demo.MESSAGES[-1]['pending'] is False


def test_new_chat_preserves_previous_messages_and_browser(monkeypatch):
    old = Mock()
    old.snapshot.return_value = {'status': 'blocked', 'page': {}}
    monkeypatch.setattr(demo, 'CHATS', {})
    monkeypatch.setattr(demo, 'CURRENT_CHAT', 'first')
    monkeypatch.setattr(demo, 'AGENT', old)
    monkeypatch.setattr(demo, 'MESSAGES', [{'role': 'user', 'content': 'First task'}])
    fresh = demo.command('new_chat', {})
    assert fresh['messages'] == [] and fresh['page'] is None
    assert fresh['chat_id'] != 'first'
    old.close.assert_not_called()
    restored = demo.command('switch_chat', {'id': 'first'})
    assert restored['messages'][0]['content'] == 'First task'
    assert demo.AGENT is old


def test_old_chat_cannot_mutate_new_chat(monkeypatch):
    import pytest
    monkeypatch.setattr(demo, 'CURRENT_CHAT', 'new')
    with pytest.raises(ValueError, match='چت فعال'):
        demo.command('tick', {'chat_id': 'old'})


def test_missing_field_asks_immediately_without_summary_model(monkeypatch):
    agent = Mock()
    agent.state = {'input_question': 'کد تأیید چیست؟'}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])
    explain = Mock(side_effect=AssertionError('No model needed'))
    monkeypatch.setattr(demo, 'explain_result', explain)
    demo.finish_message()
    assert demo.MESSAGES == [{'role': 'assistant', 'content': 'کد تأیید چیست؟'}]
    explain.assert_not_called()


def test_crash_report_visible_before_llm_and_survives_llm_failure(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    agent = Mock()
    agent.state = {'status': 'blocked'}
    agent.snapshot.return_value = {
        'status': 'blocked', 'phase': 'مشاهدهٔ نتیجهٔ اکشن',
        'page': {'url': 'https://example.test/result?secret=hidden'},
        'history': [{'action': 'Search', 'kind': 'click'}],
    }
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [])

    def fail(*args):
        entered.set()
        assert release.wait(2)
        raise RuntimeError('LLM unavailable')

    monkeypatch.setattr(demo, 'explain_result', fail)
    worker = demo.finish_message('Connection failed')
    try:
        assert entered.wait(1)
        report = demo.MESSAGES[0]['content']
        assert 'Search' in report and 'Connection failed' in report
        assert 'تأیید نشده' in report
        assert 'secret' not in report
        assert demo.MESSAGES[0]['source'] == 'harness'
    finally:
        release.set()
        worker.join(2)
    assert demo.MESSAGES[0]['content'].startswith(report)
    assert not demo.MESSAGES[0]['pending']


def test_unexpected_browser_exception_is_reported(monkeypatch):
    import pytest
    agent = Mock()
    agent.state = {'status': 'ready', 'phase': 'اجرای اکشن مرورگر'}
    agent.command.side_effect = KeyError('missing target')
    monkeypatch.setattr(demo, 'AGENT', agent)
    report = Mock()
    monkeypatch.setattr(demo, 'finish_message', report)
    with pytest.raises(KeyError):
        demo.command('tick', {})
    assert agent.state['status'] == 'blocked'
    report.assert_called_once()
    assert 'missing target' in agent.state['last_error']


def test_error_response_includes_report_and_classifies_connection(monkeypatch):
    agent = Mock()
    agent.state = {'status': 'blocked'}
    agent.snapshot.return_value = {'status': 'blocked', 'history': []}
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'MESSAGES', [{'role': 'assistant', 'content': 'failure evidence'}])
    result = demo.failure_response(TimeoutError('Connection timed out'))
    assert result['code'] == 'connection_failure'
    assert result['state']['status'] == 'blocked'
    assert result['state']['messages'][0]['content'] == 'failure evidence'
    agent.command.assert_not_called()


def test_progress_review_is_parallel_and_discards_outdated_page(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    agent = Mock()
    agent.state = {'status': 'ready', 'goal': 'Find destination', 'started_at': 1,
                   'history': [{}] * 5, 'page': {'fingerprint': 'before'}}
    agent.snapshot.side_effect = lambda: dict(agent.state)
    monkeypatch.setattr(demo, 'AGENT', agent)
    def review(snapshot):
        entered.set()
        assert release.wait(2)
        return {'action': 'continue', 'next_goal': 'Choose the visible suggestion', 'message': 'checking'}
    monkeypatch.setattr(demo, 'review_stop', review)
    worker = demo.check_progress()
    try:
        assert entered.wait(1)
        assert demo.LOCK.acquire(blocking=False)
        demo.LOCK.release()
        assert demo.check_progress() is None
        agent.state['page'] = {'fingerprint': 'after'}
    finally:
        release.set()
        worker.join(2)
    assert 'working_goal' not in agent.state
    assert agent.state['diagnostics'][-1]['event'] == 'outdated_progress_review'
    assert not agent.state['progress_check_pending']


def test_progress_review_applies_to_unchanged_page(monkeypatch):
    agent = Mock()
    agent.state = {'status': 'ready', 'goal': 'Original task', 'started_at': 1,
                   'history': [{}] * 5, 'page': {'fingerprint': 'same'}}
    agent.snapshot.side_effect = lambda: dict(agent.state)
    monkeypatch.setattr(demo, 'AGENT', agent)
    monkeypatch.setattr(demo, 'review_stop', lambda _: {
        'action': 'continue', 'next_goal': 'Select matching suggestion', 'message': 'Continue'})
    worker = demo.check_progress()
    worker.join(2)
    assert agent.state['working_goal'].startswith('Original task\n')
    assert 'Select matching suggestion' in agent.state['working_goal']
