"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import MissingFieldValue, action_space, choose, field_context, field_text
from .questions import MAX_STEPS


def repeated_cycle(history):
    """Catch repeated navigation cycles even when each click changes the page."""
    for width in (1, 2, 3):
        if len(history) < width * 3:
            continue
        recent = history[-width * 3:]
        if any(h.get("kind") not in {"click", "select"} for h in recent):
            continue
        keys = [(h.get("kind"), h.get("action"), h.get("url")) for h in recent]
        if keys[:width] == keys[width:width * 2] == keys[width * 2:]:
            return True
    return False


def empty_app_shell(page):
    return len(page.get("text", "").strip()) < 200 and not any(
        a["kind"] in {"click", "fill", "select"} for a in page.get("actions", [])
    )


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage as error:
                state["last_error"] = str(error)
                state["stale_count"] = state.get("stale_count", 0) + 1
                state.setdefault("diagnostics", []).append({
                    "event": "action_not_executed", "reason": str(error),
                    "decision_count": len(state["decisions"]),
                })
                state["decision"] = None
                state["status"] = "blocked" if state["stale_count"] >= 3 else "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            state["phase"] = "مشاهده و انتخاب قدم بعدی"
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            if empty_app_shell(state["page"]):
                state["phase"] = "انتظار برای بارگذاری کنترل‌های صفحه"
                deadline = time.perf_counter() + 6
                polls = 0
                while empty_app_shell(state["page"]) and time.perf_counter() < deadline:
                    time.sleep(0.15)
                    state["page"] = state["browser"].observe(screenshot=False)
                    polls += 1
                if self.screenshots:
                    state["page"] = state["browser"].observe(screenshot=True)
                state.setdefault("diagnostics", []).append({
                    "event": "app_shell_wait", "polls": polls,
                    "controls_ready": not empty_app_shell(state["page"]),
                })
                state["phase"] = "مشاهده و انتخاب قدم بعدی"
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            state["decision"] = choose(state["page"], state.get("working_goal", state["goal"]), state["history"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                # Terminal choices do not mutate a DOM target. Map/clock updates must not
                # turn a stop into a stale-target failure; verify the document, then observe.
                if not state["browser"].fresh(page, {"kind": "wait"}):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page, action):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state.get("working_goal", state["goal"]), action, page, state["history"])
                context["user_message"] = state.get("user_message", "")
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    state["phase"] = "تولید متن فیلد"
                    try:
                        text, helper = field_text(context)
                    except ValueError as error:
                        missing = isinstance(error, MissingFieldValue)
                        state.update(status="blocked", last_error=str(error))
                        state.setdefault("diagnostics", []).append({
                            "event": "missing_field_value" if missing else "invalid_field_response",
                            "field": action["label"], "decision_count": len(state["decisions"]),
                        })
                        if missing:
                            state["input_question"] = f'برای فیلد «{action["label"][:160]}» چه مقداری وارد کنم؟'
                        raise
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            state["phase"] = "اجرای اکشن مرورگر"
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["stale_count"] = 0
            state.pop("last_error", None)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["phase"] = "مشاهدهٔ نتیجهٔ اکشن"
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["phase"] = "ارزیابی پیشرفت پس از مشاهدهٔ صفحه"
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
            if repeated_cycle(state["history"]):
                state["status"] = "blocked"
                state["last_error"] = "Repeated navigation cycle; no verified progress toward the task."
                state.setdefault("diagnostics", []).append(
                    {"event": "navigation_cycle", "steps": len(state["history"])}
                )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def continue_goal(self, goal):
        """Start a new conversational turn on the existing page, without navigating away."""
        page = self.browser.observe(screenshot=self.screenshots)
        self.pending_text = None
        self.state.update(
            goal=goal, plan=[goal], plan_index=0, page=page, decision=None,
            status="ready", started_at=None, elapsed_ms=0, history=[], decisions=[], text_calls=[],
            progress_checked_at=0,
        )
        self.state.pop("reported", None)
        self.state.pop("last_error", None)
        self.state.pop("input_question", None)
        self.state.pop("working_goal", None)
        self.state.pop("recovery_goals", None)
        self.state["stale_count"] = 0
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
