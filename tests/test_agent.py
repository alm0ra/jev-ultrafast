"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_repeated_stale_decisions_stop_with_diagnostics(runner):
    runner.state['browser'].fresh.side_effect = StalePage('Target covered')
    for _ in range(3):
        runner.command('tick')
    assert runner.state['status'] == 'blocked'
    assert runner.state['last_error'] == 'Target covered'
    assert len(runner.state['diagnostics']) == 3
    runner.state['browser'].act.assert_not_called()


def test_continue_goal_keeps_existing_browser(runner):
    browser = runner.state["browser"]
    runner.browser = browser
    runner.state['status'] = 'blocked'
    runner.state['reported'] = True
    runner.continue_goal('Use the information supplied in the follow-up')
    assert runner.browser is browser
    browser.close.assert_not_called()
    assert runner.state['status'] == 'ready'
    assert runner.state['goal'] == 'Use the information supplied in the follow-up'
    assert 'reported' not in runner.state


def test_missing_field_blocks_without_typing_and_preserves_evidence(runner, monkeypatch):
    monkeypatch.setattr(loop, 'field_text', Mock(side_effect=model.MissingFieldValue('Missing; nothing typed')))
    runner.state['user_message'] = 'Search for a book'
    with pytest.raises(model.MissingFieldValue):
        runner.command('act', {'fingerprint': runner.state['page']['fingerprint']})
    runner.state['browser'].act.assert_not_called()
    assert runner.state['status'] == 'blocked'
    assert 'Search' in runner.state['input_question']
    assert runner.state['diagnostics'][-1]['event'] == 'missing_field_value'


def test_empty_choice_list_is_reported_as_invalid_field(monkeypatch):
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'test')
    monkeypatch.setattr(model, 'post_json', Mock(return_value={'choices': []}))
    with pytest.raises(ValueError, match='nothing typed'):
        model.field_text({'goal': 'Find a book'})


def test_pending_autocomplete_cannot_skip_to_next_field_or_submit(monkeypatch):
    p = page()
    p['actions'].append({'id': 'suggestion', 'node': 40, 'kind': 'click', 'role': 'option', 'label': 'Tehran'})
    p['interaction'] = {'kind': 'autocomplete', 'selection_pending': True, 'field_node': 10,
                        'option_nodes': [40], 'query': 'Teh'}
    def post(url, key, body):
        questions = body['questions']
        assert 'DONE' not in questions['operation']['criteria']
        click = questions['click_target']['criteria']
        assert len(click) == 1
        assert next(iter(click.values()))['element'].endswith('Tehran')
        target = next(iter(click))
        return {'model': 'test', 'answers': {
            'operation': choice(questions['operation']['criteria'], 'CLICK'),
            'click_target': choice(click, target)}}
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test')
    monkeypatch.setattr(model, 'post_json', post)
    assert model.choose(p, 'Select Tehran then search', [])['choice'] == 'suggestion'


def test_detects_navigation_cycle_despite_changing_pages():
    pair = [{'kind': 'click', 'action': 'Destination', 'url': '/search', 'page_changed': True},
            {'kind': 'click', 'action': 'Back', 'url': '/ride', 'page_changed': True}]
    assert loop.repeated_cycle(pair * 3)
    assert not loop.repeated_cycle(pair * 2)
    assert not loop.repeated_cycle([{'kind': 'scroll', 'action': 'Down', 'url': '/results'}] * 6)


def test_terminal_stop_observes_latest_page_without_dom_target_guard(runner):
    runner.state['decision'] = decision('BLOCKED')
    runner.command('act', {'fingerprint': runner.state['page']['fingerprint']})
    runner.state['browser'].fresh.assert_called_once_with(runner.state['page'], {'kind': 'wait'})
    runner.state['browser'].observe.assert_called_once()
    runner.state['browser'].act.assert_not_called()
    assert runner.state['status'] == 'blocked'


def test_app_shell_is_reobserved_before_model_decision(runner, monkeypatch):
    shell = {**page(), 'text': 'v18.44.1', 'actions': [{'id': 'wait', 'kind': 'wait', 'label': 'Wait'}]}
    runner.state['page'] = shell
    runner.state['status'] = 'ready'
    loaded = page()
    runner.state['browser'].observe.return_value = loaded
    monkeypatch.setattr(loop.time, 'sleep', lambda _: None)
    choose = Mock(return_value=decision())
    monkeypatch.setattr(loop, 'choose', choose)
    runner.command('predict')
    assert choose.call_args.args[0] == loaded
    assert runner.state['diagnostics'][-1]['controls_ready'] is True
    runner.state['browser'].act.assert_not_called()


def test_app_shell_wait_is_bounded(runner, monkeypatch):
    shell = {**page(), 'text': 'v18.44.1', 'actions': []}
    runner.state['page'] = shell
    runner.state['status'] = 'ready'
    runner.state['browser'].observe.return_value = shell
    monkeypatch.setattr(loop.time, 'sleep', lambda _: None)
    clock = iter([10, 11, 20, 21])
    monkeypatch.setattr(loop.time, 'perf_counter', lambda: next(clock))
    monkeypatch.setattr(loop, 'choose', Mock(return_value=decision('BLOCKED')))
    runner.command('predict')
    assert runner.state['diagnostics'][-1] == {'event': 'app_shell_wait', 'polls': 1, 'controls_ready': False}
