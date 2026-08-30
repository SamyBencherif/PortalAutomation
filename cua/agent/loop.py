"""The discovery loop: observe -> decide -> act, with a model in the loop.

This runs once per capability, ever. Its job is not to be the production path --
that is `replay`, which never calls a model -- but to find out how the flow
works and leave behind enough structure for the recorder to turn into an
artifact.

The model gets the computer toolset and nothing else. No DOM query tool, no
"navigate to URL" helper, no hints about the app: it sees screenshots and moves
a mouse, exactly like the operator it is standing in for. That constraint is
the point. A discovery run that can cheat with selectors produces an artifact
that only replays where selectors exist, which is precisely the surface we do
not have in the real environment.

Two safety properties hold even though a model is choosing the actions:

- Every request the browser makes goes through the policy proxy, so the model
  cannot reach a denied route no matter what it decides to click.
- The loop is bounded on steps AND wall-clock, because "the model will stop
  when it is done" is not a termination argument.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from cua.artifact.schema import Point
from cua.evidence.run import RunLog
from cua.perception import ocr
from cua.replay.outcomes import OutcomeClass, classify
from cua.surface.base import Frame, Surface, SurfaceError

MODEL = "claude-opus-5"

# Server-side refusal fallback. Driving a bank UI -- even a fabricated practice
# one -- sits close enough to the `cyber` policy that the classifier declines
# intermittently: the identical goal completed in 9 actions on one run and was
# refused after 5 on the next. Without this a run simply stops mid-flow with no
# recourse. `"default"` routes by refusal category, so there is no model list to
# maintain, and the API's own refusal message recommends exactly this.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The current toolset. Note it carries NO display_width_px/display_height_px --
# those were removed; coordinates live in the pixel space of the screenshots we
# return. It needs no beta header.
COMPUTER_TOOL = {
    "type": "computer_toolset_20260801",
    "configs": {"zoom": {"enabled": True}},
}
TOOLSET = "computer"

SYSTEM = """You are exercising a UI automation system against `mock_teller`, a \
practice application that ships with this project's own source code and runs \
in a local container. Every record in it is invented: the names, account \
numbers and SSN-shaped fields are fabricated test fixtures, and there is no \
institution behind it.

A session is already established for you -- you do not need to sign in, and \
you will not be asked to handle any credentials. Start from the screen in \
front of you.

You are operating the application through a remote desktop, as a human teller \
would. You can only see screenshots and you can only click, type, scroll and \
press keys.

This is a legacy application. Expect:
- small text, table layouts, and controls whose label sits in the cell to the LEFT
- pages that finish loading before their data arrives
- occasional maintenance notices covering the page, which must be dismissed
- error banners carrying reference codes like E-403-PROFILE

Work in small steps. Take a screenshot after anything that could change the \
screen, and read it before deciding what to do next. Never guess that a click \
worked -- verify it on screen.

If you reach a state you cannot resolve (a supervisor code you were not given, \
a permission denial, an application fault), STOP and say so plainly rather than \
trying alternatives. Stopping is a correct outcome; a human will take over.

When the goal is achieved, state the answer and stop."""


@dataclass
class Decision:
    """One action the model chose, and the screen it chose it from.

    The pre-action frame is kept because that is what the recorder anchors
    against: to turn "clicked at (512, 300)" into "the box to the right of
    'Nickname'", you need the screen as it was when the click was decided.
    """

    action: str
    args: dict[str, Any]
    frame_before: Frame
    step: int


@dataclass
class DiscoveryResult:
    goal: str
    status: str                       # succeeded | stopped | exhausted | failed
    trajectory: list[Decision] = field(default_factory=list)
    final_text: str = ""
    steps: int = 0
    stop_reason: str = ""
    error: str | None = None
    # True if any turn was served by a refusal fallback rather than the model
    # that was asked for. Surfaced so a run's provenance is not overstated.
    used_fallback: bool = False


class AgentLoop:
    def __init__(
        self,
        surface: Surface,
        log: RunLog,
        model: str = MODEL,
        max_steps: int = 40,
        max_seconds: float = 600.0,
        client: Any = None,
    ) -> None:
        self.surface = surface
        self.log = log
        self.model = model
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        # Injectable so the loop can be exercised without a live API key.
        self.client = client or anthropic.Anthropic()
        self.trajectory: list[Decision] = []

    # ------------------------------------------------------------ plumbing

    def _shot(self) -> tuple[Frame, dict[str, Any]]:
        frame = self.surface.observe()
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(frame.png).decode("ascii"),
            },
        }
        return frame, block

    def _dispatch(self, name: str, args: dict[str, Any], step: int) -> list[dict[str, Any]]:
        """Execute one toolset member. Only screenshot and zoom return images."""
        point = None
        if isinstance(args.get("coordinate"), (list, tuple)):
            point = Point(x=int(args["coordinate"][0]), y=int(args["coordinate"][1]))
        mods = args.get("text") if name.endswith("click") or name == "scroll" else None

        match name:
            case "screenshot":
                _, block = self._shot()
                return [block]

            case "zoom":
                # Crop rather than re-render: the model asked to look closer at
                # a region of the frame we already have.
                x0, y0, x1, y1 = (int(v) for v in args["region"])
                frame = self.surface.observe()
                import io

                from PIL import Image
                with Image.open(io.BytesIO(frame.png)) as img:
                    crop = img.crop((x0, y0, x1, y1))
                    buf = io.BytesIO()
                    crop.save(buf, format="PNG")
                return [{
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png",
                               "data": base64.b64encode(buf.getvalue()).decode("ascii")},
                }]

            case "left_click" | "right_click" | "middle_click":
                button = {"left_click": "left", "right_click": "right",
                          "middle_click": "middle"}[name]
                self.surface.click(point or self.surface.cursor_position(), button, mods)
            case "double_click":
                self.surface.double_click(point or self.surface.cursor_position(), mods)
            case "triple_click":
                self.surface.triple_click(point or self.surface.cursor_position(), mods)
            case "left_click_drag":
                start = args["start_coordinate"]
                self.surface.drag(Point(x=int(start[0]), y=int(start[1])),
                                  point or self.surface.cursor_position(), mods)
            case "mouse_move":
                self.surface.move(point)
            case "left_mouse_down":
                self.surface.mouse_down()
            case "left_mouse_up":
                self.surface.mouse_up()
            case "cursor_position":
                pos = self.surface.cursor_position()
                return [{"type": "text", "text": f"X={pos.x}, Y={pos.y}"}]
            case "scroll":
                self.surface.scroll(point, args.get("scroll_direction", "down"),
                                    int(args.get("scroll_amount", 3)), mods)
            case "type":
                self.surface.type_text(args.get("text", ""))
            case "key":
                self.surface.key(args.get("text", "Return"), int(args.get("repeat", 1)))
            case "hold_key":
                self.surface.hold_key(args.get("text", ""), float(args.get("duration", 1)))
            case "wait":
                self.surface.wait(float(args.get("duration", 1)))
            case _:
                return [{"type": "text", "text": f"Unsupported action {name!r}"}]

        return [{"type": "text", "text": "OK"}]

    # ---------------------------------------------------------------- loop

    def run(self, goal: str, entry_url: str) -> DiscoveryResult:
        result = DiscoveryResult(goal=goal, status="failed")
        self.log.event("discovery_started", goal=goal, model=self.model,
                       entry_url=entry_url, max_steps=self.max_steps)

        try:
            self.surface.navigate(entry_url)
            self.surface.wait(1.5)
        except SurfaceError as e:
            result.error = str(e)
            self.log.event("surface_error", error=str(e))
            return result

        frame, shot = self._shot()
        self.log.frame(frame, "discovery-start")
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}\n\nHere is the screen."},
                shot,
            ],
        }]

        deadline = time.time() + self.max_seconds
        step = 0

        while step < self.max_steps and time.time() < deadline:
            try:
                response = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=SYSTEM,
                    tools=[COMPUTER_TOOL],
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    messages=messages,
                )
            except anthropic.APIError as e:
                result.error = f"{type(e).__name__}: {e}"
                result.status = "failed"
                self.log.event("model_error", error=result.error)
                return result

            messages.append({"role": "assistant", "content": response.content})

            # Record when a fallback actually served the turn. A run rescued by
            # a different model is a materially different run, and the evidence
            # should say so rather than quietly look clean.
            if any(
                getattr(entry, "type", None) == "fallback_message"
                for entry in (getattr(response.usage, "iterations", None) or [])
            ):
                self.log.event("served_by_fallback", served_by=response.model)
                result.used_fallback = True

            text = " ".join(b.text for b in response.content if b.type == "text").strip()
            if text:
                self.log.event("model_said", text=text[:800])

            if response.stop_reason != "tool_use":
                # The model is finished, one way or the other. Whether the goal
                # was actually met is decided by the caller against a
                # checkpoint, not by taking the model's word for it.
                result.final_text = text
                result.stop_reason = response.stop_reason or ""
                result.status = "stopped" if response.stop_reason == "refusal" else "succeeded"
                if response.stop_reason == "refusal":
                    # stop_details is populated ONLY on a refusal and carries
                    # the category. Without logging it, a declined run looks
                    # identical to a model that simply stopped early, and there
                    # is nothing to act on.
                    details = getattr(response, "stop_details", None)
                    result.error = (
                        f"refused: {getattr(details, 'category', None)} "
                        f"-- {getattr(details, 'explanation', None)}"
                    )
                    self.log.event(
                        "model_refused",
                        category=getattr(details, "category", None),
                        explanation=getattr(details, "explanation", None),
                        said=text[:500],
                    )
                break

            results: list[dict[str, Any]] = []
            failed = False
            for block in response.content:
                if block.type != "tool_use":
                    continue
                step += 1
                name = block.name
                args = dict(block.input or {})

                if failed:
                    # Per the toolset contract: once one action in a turn
                    # fails, the rest are not executed.
                    results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "toolset_name": TOOLSET,
                        "content": [{"type": "text", "text":
                                     "Not executed: an earlier computer action "
                                     "in this turn failed."}],
                    })
                    continue

                before = self.surface.observe()
                self.log.event("agent_action", step=step, action=name,
                               args={k: v for k, v in args.items() if k != "text"}
                               if name == "type" else args)
                try:
                    content = self._dispatch(name, args, step)
                except (SurfaceError, KeyError, ValueError) as e:
                    failed = True
                    content = [{"type": "text", "text": f"Action failed: {e}"}]
                    self.log.event("action_failed", step=step, action=name, error=str(e))
                else:
                    if name not in ("screenshot", "zoom", "cursor_position"):
                        self.trajectory.append(
                            Decision(action=name, args=args, frame_before=before, step=step)
                        )

                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "toolset_name": TOOLSET, "content": content,
                })

            messages.append({"role": "user", "content": results})

            # Give the model a fresh look after acting, and notice on our own
            # account if the app has said something terminal.
            frame = self.surface.observe()
            screen = ocr.read(frame.png)
            sig = classify(screen)
            if sig is not None and sig.klass in (OutcomeClass.HARD, OutcomeClass.STUCK):
                self.log.frame(frame, f"discovery-{sig.code.lower()}")
                self.log.event("discovery_blocked", code=sig.code, message=sig.message)
                result.status = "stopped"
                result.stop_reason = sig.code
                break
        else:
            result.status = "exhausted"
            result.stop_reason = (
                "max_steps" if step >= self.max_steps else "timeout"
            )

        frame = self.surface.observe()
        self.log.frame(frame, "discovery-final")
        result.trajectory = self.trajectory
        result.steps = step
        self.log.event("discovery_finished", status=result.status, steps=step,
                       stop_reason=result.stop_reason)
        return result


__all__ = ["AgentLoop", "DiscoveryResult", "Decision", "COMPUTER_TOOL", "MODEL", "SYSTEM"]
