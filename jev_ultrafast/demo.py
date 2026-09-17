"""Loopback-only inspector for the Jev browser agent."""

import atexit
import json
import os
import secrets
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .agent import Agent
from .model import api_metrics, chat_plan, explain_result, review_stop, starting_url
from .questions import MAX_STEPS

ROOT = Path(__file__).parent
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT = None
MESSAGES = []
CURRENT_CHAT = secrets.token_hex(8)
CHATS = {}
LAST_STATE = {"status": "idle", "page": None, "messages": [], "history": []}


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def response_state():
    global LAST_STATE
    CHATS[CURRENT_CHAT] = {"agent": AGENT, "messages": MESSAGES}
    chats = [{"id": key, "title": next(
        (m["content"][:60] for m in chat["messages"] if m["role"] == "user"), "چت جدید"
    )} for key, chat in CHATS.items()]
    state = AGENT.snapshot() if AGENT else {"page": None, "status": "idle", "history": [], "decision": None}
    result = {**state, "messages": MESSAGES, "chat_id": CURRENT_CHAT, "chats": chats,
              "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"),
              "max_steps": MAX_STEPS, "performance": api_metrics()}
    LAST_STATE = deepcopy(result)
    return result


def close_browser():
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def failure_report(snapshot, error=None):
    """A usable report from executor evidence, independent of model availability."""
    history = snapshot.get("history", [])
    page = snapshot.get("page") or {}
    location = urlparse(page.get("url", ""))
    lines = [f'اجرا متوقف شد؛ {len(history)} اکشن ثبت شده است. تکمیل درخواست تأیید نشده است.']
    if location.hostname:
        lines.append(f'آخرین صفحهٔ مشاهده‌شده: {location.hostname}{location.path}')
    if history:
        lines.append('آخرین کارهای ثبت‌شده: ' + ' ← '.join(str(h.get("action", h.get("kind", "")))[:100]
                                                          for h in history[-3:]))
    phase = snapshot.get("phase", "نامشخص")
    lines.append(f'مرحلهٔ توقف: {phase}')
    reason = error or snapshot.get("last_error")
    if reason:
        lines.append('علت ثبت‌شده: ' + str(reason)[:400])
    else:
        lines.append('اجرا قدم بعدی قابل اجرا پیدا نکرد؛ این به معنی نبودن نتیجه در سایت نیست.')
    if phase == "مشاهدهٔ نتیجهٔ اکشن":
        lines.append('اکشن ارسال شده، اما نتیجه‌اش تأیید نشده؛ پیش از تکرار باید وضعیت صفحه بررسی شود.')
    if snapshot.get("input_question"):
        lines.append(snapshot["input_question"])
    else:
        lines.append('برای ادامه در همین تب بنویس «ادامه بده»؛ وضعیت صفحه دوباره بررسی می‌شود.')
    return '\n'.join(lines)


def failure_response(error):
    """Return the stopped execution with the error so the UI cannot lose its report."""
    detail = str(error) or type(error).__name__
    lower = detail.lower()
    if not AGENT or AGENT.state.get("status") != "blocked":
        message = detail if isinstance(error, ValueError) else "شروع درخواست ناموفق بود؛ دوباره درخواستت را بفرست."
        return {"error": message, "code": "request_error", "state": response_state()}
    if AGENT and AGENT.state.get("input_question"):
        code, message = "input_required", AGENT.state["input_question"]
    elif "budget" in lower:
        code, message = "budget_exhausted", "سقف این اجرا تمام شد؛ گزارش و کارهای انجام‌شده در چت هستند."
    elif any(word in lower for word in ("timeout", "timed out", "connection", "connect", "429", "503")):
        code, message = "connection_failure", "ارتباط سرویس قطع یا کند شد؛ وضعیت اجرا و راه ادامه در چت آمده است."
    elif "page changed" in lower or "target" in lower:
        code, message = "browser_changed", "وضعیت صفحه یا تب تغییر کرد؛ برای ادامه باید دوباره بررسی شود."
    elif "field value" in lower:
        code, message = "invalid_field_value", "پاسخ مدل برای تایپ معتبر نبود؛ در این مرحله چیزی تایپ نشد."
    else:
        code, message = "execution_error", "اجرای درخواست ناموفق بود؛ جزئیات در گزارش اجرا آمده است."
    return {"error": message, "code": code, "state": response_state()}


def check_progress():
    """Review in parallel; apply advice only to the exact observed page and task."""
    if not AGENT or AGENT.state.get("status") != "ready":
        return
    agent = AGENT
    state = agent.state
    count = len(state.get("history", []))
    if state.get("progress_check_pending") or count - state.get("progress_checked_at", 0) < 5:
        return
    snapshot = deepcopy(agent.snapshot())
    state.update(progress_check_pending=True, progress_checked_at=count)

    def review():
        try:
            advice = review_stop(snapshot)
            with LOCK:
                current = agent.state
                same_task = current.get("started_at") == snapshot.get("started_at")
                same_page = (current.get("page") or {}).get("fingerprint") == (
                    snapshot.get("page") or {}).get("fingerprint")
                if AGENT is agent and same_task and same_page and current.get("status") == "ready":
                    if advice["action"] == "continue":
                        current["working_goal"] = current["goal"] + '\nCURRENT NEXT STEP: ' + advice["next_goal"]
                    current.setdefault("diagnostics", []).append({
                        "event": "progress_review", "step": count, "action": advice["action"],
                        "message": advice["message"],
                    })
                else:
                    current.setdefault("diagnostics", []).append({"event": "outdated_progress_review", "step": count})
        except Exception:
            with LOCK:
                agent.state.setdefault("diagnostics", []).append(
                    {"event": "progress_review_unavailable", "step": count}
                )
        finally:
            with LOCK:
                agent.state["progress_check_pending"] = False

    worker = threading.Thread(target=review, name="progress-review", daemon=True)
    worker.start()
    return worker


def finish_message(error=None):
    if not AGENT or AGENT.state.get("reported"):
        return
    agent = AGENT
    if agent.state.get("input_question"):
        agent.state["reported"] = True
        MESSAGES.append({"role": "assistant", "content": agent.state["input_question"]})
        return
    snapshot = deepcopy(agent.snapshot())
    agent.state["reported"] = True
    fallback = failure_report(snapshot, error)
    stopped = bool(error) or snapshot.get("status") == "blocked"
    message = {"role": "assistant", "content": fallback if stopped else "در حال بررسی نتیجهٔ اجرا…",
               "source": "harness", "pending": True}
    MESSAGES.append(message)

    def report():
        try:
            if not error and snapshot.get("status") in {"done", "blocked"} and len(
                snapshot.get("recovery_goals", [])
            ) < 2:
                review = review_stop(snapshot)
                if review["action"] == "continue":
                    with LOCK:
                        if AGENT is agent and agent.state.get("goal") == snapshot.get("goal") and (
                            agent.state.get("started_at") == snapshot.get("started_at")
                        ) and (
                            agent.state.get("status") in {"done", "blocked"}
                        ):
                            goals = agent.state.setdefault("recovery_goals", [])
                            goals.append(review["next_goal"])
                            agent.state.update(
                                working_goal=agent.state["goal"] + "\nCURRENT NEXT STEP: " + review["next_goal"],
                                status="ready", decision=None, stale_count=0,
                            )
                            agent.state.pop("reported", None)
                            message.update(content=review["message"], pending=False)
                            return
                answer = review["message"] if review["action"] == "stop" else explain_result(snapshot, error)
            else:
                answer = explain_result(snapshot, error)
        except Exception:
            answer = fallback + '\nتوضیح تکمیلی مدل ناموفق بود؛ گزارش بالا از اطلاعات خود اجراست.'
        # The network request never holds the browser lock. Only publish the finished text here.
        with LOCK:
            message.update(content=(fallback + '\n\n' + answer) if stopped and answer != fallback
                           and not answer.startswith(fallback) else answer, pending=False)

    worker = threading.Thread(target=report, name="result-explanation", daemon=True)
    worker.start()
    return worker


def command(name, body):
    global AGENT, MESSAGES, CURRENT_CHAT
    if name not in {"new_chat", "switch_chat"} and body.get("chat_id", CURRENT_CHAT) != CURRENT_CHAT:
        raise ValueError("چت فعال عوض شده؛ صفحه را تازه کن و دوباره از چت موردنظر ادامه بده.")
    if name in {"new_chat", "switch_chat"}:
        response_state()
        if name == "new_chat":
            CURRENT_CHAT = secrets.token_hex(8)
            AGENT, MESSAGES = None, []
        else:
            selected = body.get("id")
            if selected not in CHATS:
                raise ValueError("Chat not found")
            CURRENT_CHAT = selected
            AGENT, MESSAGES = CHATS[selected]["agent"], CHATS[selected]["messages"]
        return response_state()
    if name == "chat":
        message = body.get("message", "").strip()
        if not 0 < len(message) <= 2000:
            raise ValueError("Enter 1–2,000 characters")
        MESSAGES.append({"role": "user", "content": message})
        page = AGENT.state.get("page", {}) if AGENT else {}
        context = {"url": page.get("url"), "text": page.get("text", "")[:10000]}
        try:
            plan = chat_plan(MESSAGES, context)
            if plan["action"] == "reply":
                MESSAGES.append({"role": "assistant", "content": plan["message"]})
                return {**response_state(), "start_run": False}
            if plan["action"] == "continue":
                if AGENT is None:
                    raise ValueError("جلسهٔ مرورگر قبلی در دسترس نیست؛ درخواست اصلی را دوباره بفرست.")
                AGENT.continue_goal(plan["goal"])
                result = response_state()
            else:
                result = command("reset", {
                    "scenario": "custom", "goal": plan["goal"],
                    "url": "https://www.google.com/search?" + urlencode({"q": plan["query"]}),
                })
            AGENT.state["user_message"] = message
            MESSAGES.append({"role": "assistant", "content": plan["message"] or "جست‌وجو را شروع می‌کنم."})
            return {**result, "messages": MESSAGES, "start_run": True}
        except (ValueError, RuntimeError, TimeoutError) as error:
            MESSAGES.append({"role": "assistant", "content": "شروع درخواست ناموفق بود: " + str(error)})
            raise
    if name == "summary":
        finish_message()
        return response_state()
    if name == "reset":
        scenario = body.get("scenario", "custom")
        if scenario not in {"custom", "travel", "research", "flights"}:
            raise ValueError("Unknown demo scenario")
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        url = (
            starting_url(goal, body.get("url", "")) if scenario == "custom"
            else "https://www.google.com/travel/flights?hl=en" if scenario == "flights"
            else f"{ORIGIN}/fixture.html?scenario={scenario}"
        )
        close_browser()
        AGENT = Agent(
            url,
            goal,
            screenshots=True,
            record_dir=Path.cwd() / "artifacts" / "frames" if body.get("record") else None,
        )
        AGENT.state["scenario"] = scenario
    else:
        if AGENT is None:
            raise ValueError("Start a demo first")
        if name in {"tick", "predict", "act"}:
            AGENT.state.pop("reported", None)
        try:
            AGENT.command(name, body)
        except Exception as error:
            AGENT.state.update(status="blocked", last_error=str(error) or type(error).__name__)
            finish_message(str(error))
            raise
        if AGENT.state["status"] in {"done", "blocked"}:
            finish_message()
        elif name in {"tick", "act"}:
            check_progress()
    return response_state()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/activity":
            return self.send(200, json.dumps({"busy": LOCK.locked()}))
        if path == "/api/state":
            if not LOCK.acquire(blocking=False):
                return self.send(200, json.dumps({**LAST_STATE, "busy": True}))
            try:
                return self.send(200, json.dumps({**response_state(), "busy": False}))
            finally:
                LOCK.release()
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        files = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/fixture.html": ("fixture.html", "text/html"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local demo requests only"}))
        if self.headers.get("X-Demo-Token") != TOKEN:
            return self.send(403, json.dumps({
                "error": "Server restarted. Refresh this page and start your task again.",
                "code": "stale_session",
            }))
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({
                "error": "یک عملیات در حال اجراست؛ منتظر بمان و دمو را فقط در یک تب اجرا کن."
            }))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 8192:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            result = command(self.path.removeprefix("/api/"), body)
            self.send(200, json.dumps(result))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps(failure_response(error)))
        except Exception as error:
            self.send(500, json.dumps(failure_response(error)))
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    atexit.register(close_browser)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Ultrafast: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
