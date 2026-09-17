"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def create_browser_tab():
    if sys.platform != "darwin":
        return cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    # Chrome's user-enabled debugging can reject Target.createTarget on macOS.
    # Open exactly one owned tab through Launch Services, then attach through CDP.
    with tempfile.NamedTemporaryFile(suffix=".html", prefix="jev-tab-", mode="w") as page:
        page.write("<!doctype html><title>Jev</title><p>Opening your task…</p>")
        page.flush()
        url = Path(page.name).resolve().as_uri()
        subprocess.run(["open", "-a", "Google Chrome", url], check=True, timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for target in cdp("Target.getTargets")["targetInfos"]:
                if target.get("type") == "page" and target.get("url") == url:
                    return target["targetId"]
            time.sleep(0.05)
    raise RuntimeError("Chrome did not expose the new tab within 5 seconds. Check its debugging connection.")


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.target = create_browser_tab()
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.clearDeviceMetricsOverride")
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && action.autocomplete;
                      const started=performance.now();
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 1200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        const labels=options.map(e=>(e.textContent||'').replace(/\\s+/g,' ').trim());
                        const changed=JSON.stringify(labels)!==JSON.stringify(action.suggestion_labels||[]);
                        if (++frames>=2 && (!autocomplete || (changed && performance.now()-started>=150 &&
                          options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        })))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"scroll", "wait"}:
            return self.evaluate("[performance.timeOrigin,location.href]") == page["page_key"][:2]
        if action is not None and action["kind"] in {"click", "select", "fill"}:
            node = action["node"]
            if type(node) is not int:
                return False
            field_only = action["kind"] == "fill"
            current = self.evaluate(
                "(() => { const c=window.__jevFast; if(!c) return null; const k=c.pageKey(); "
                f"return [[k[0],k[1],k[6]],c.guard(c.nodes.get({node}),{str(field_only).lower()})]; }})()"
            )
            guards = page.get("field_guards", {}) if field_only else page["guards"]
            key = page["page_key"]
            return current == [[key[0], key[1], key[6]], guards.get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            try:
                cdp("Target.closeTarget", targetId=self.target)
            except RuntimeError as error:
                if "No target with given id found" not in str(error):
                    raise
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            size = evaluate("({w:innerWidth,h:innerHeight})")
            call("Input.dispatchMouseEvent", type="mouseWheel", x=size["w"] * .5,
                 y=size["h"] * .6, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const point=window.__jevFast.clickPoint(e);
              if (!point) return null;
              const {x,y}=point;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
