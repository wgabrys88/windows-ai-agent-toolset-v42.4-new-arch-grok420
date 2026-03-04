import asyncio
import json
from types import ModuleType

CURRENT_TASK = "Draw a can"
# CURRENT_TASK = "Open Notepad and type hello world"
# CURRENT_TASK = "Search the web for latest AI news"
# CURRENT_TASK = "Take a screenshot and describe what you see"

_AGENTS: dict[str, str] = {
    "Harper": "research",
    "Benjamin": "logic",
    "Lucas": "creative",
    "Atlas": "gui",
    "Nova": "manager",
}

_AGENT_SYSTEM: dict[str, str] = {
    "Harper": """
You are Harper, Research and Facts specialist in a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Your role: gather facts, recall relevant knowledge, identify what information is needed.
Observation of current screen: {observation}
History: {history}
Respond with your analysis and concrete suggestions for next actions.
""",
    "Benjamin": """
You are Benjamin, Logic and Planning specialist in a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Your role: reason step by step, plan precise action sequences, detect errors in reasoning.
Observation of current screen: {observation}
History: {history}
Debate so far: {debate}
Respond with your logical analysis and proposed action plan.
""",
    "Lucas": """
You are Lucas, Creative and Balance specialist in a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Your role: propose alternative approaches, balance speed vs accuracy, avoid getting stuck.
Observation of current screen: {observation}
History: {history}
Debate so far: {debate}
Respond with creative alternatives and your balanced recommendation.
""",
    "Atlas": """
You are Atlas, GUI Navigation specialist in a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Your role: identify UI elements, determine exact coordinates for actions, navigate interfaces.
Coordinates are in range 0-1000 mapping to the visible screen region.
Observation of current screen: {observation}
History: {history}
Debate so far: {debate}
Respond with specific UI element locations and exact action sequences.
""",
    "Nova": """
You are Nova, Task Manager in a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Progress: {progress}%
Your role: assess completion, manage phase transitions, propose next goal when done.
Observation of current screen: {observation}
History: {history}
Debate so far: {debate}
Respond with completion assessment and phase transition recommendation.
""",
}

_CAPTAIN_SYSTEM = """
You are Grok, Captain and final decision maker of a computer-use AI swarm.
Current goal: {goal}
Phase: {phase}
Progress: {progress}%

You receive a full debate from 5 specialist agents. Synthesize it into a decision.

Respond with ONLY valid JSON, no explanation, no markdown, no code block:
{{
  "actions": [
    {{"action": "click", "x": 500, "y": 300}},
    {{"action": "type_text", "text": "hello"}},
    {{"action": "press_key", "key": "enter"}},
    {{"action": "hotkey", "keys": "ctrl+s"}},
    {{"action": "scroll_up", "x": 500, "y": 500}},
    {{"action": "scroll_down", "x": 500, "y": 500}},
    {{"action": "double_click", "x": 400, "y": 200}},
    {{"action": "right_click", "x": 400, "y": 200}},
    {{"action": "drag", "x1": 100, "y1": 100, "x2": 600, "y2": 400}},
    {{"action": "wait"}}
  ],
  "is_complete": false,
  "next_goal": null,
  "progress": 0
}}

actions array may be empty if waiting is appropriate.
is_complete true only when goal is fully achieved.
next_goal string only when is_complete is true, otherwise null.
progress integer 0-100.
"""


def _make_progress_overlay(phase: str, progress: int, goal: str) -> list[dict]:
    bar = max(1, progress * 9)
    return [
        {
            "points": [[80, 80], [920, 80], [920, 920], [80, 920]],
            "closed": True,
            "stroke": "#00ff88",
            "fill": "",
            "label": f"PHASE:{phase}",
            "label_position": [85, 83],
            "label_style": {"font_size": 11, "bg": "#000000", "color": "#00ff88", "align": "left"},
        },
        {
            "points": [[50, 30]],
            "closed": False,
            "stroke": "#ffffff",
            "fill": "",
            "label": goal[:60],
            "label_position": [50, 30],
            "label_style": {"font_size": 14, "bg": "#000000", "color": "#ffffff", "align": "left"},
        },
        {
            "points": [[50, 55], [50 + bar, 55]],
            "closed": False,
            "stroke": "#00ffff",
            "fill": "",
            "label": f"{progress}%",
            "label_position": [50 + bar + 4, 50],
            "label_style": {"font_size": 10, "bg": "", "color": "#00ffff", "align": "left"},
        },
    ]


def _dispatch_action(hub: ModuleType, action: dict) -> None:
    act = str(action.get("action", "")).lower()
    x = int(action.get("x", 500))
    y = int(action.get("y", 500))
    match act:
        case "click":
            hub.actions(hub.click(x, y))
        case "double_click":
            hub.actions(hub.double_click(x, y))
        case "right_click":
            hub.actions(hub.right_click(x, y))
        case "type_text":
            hub.actions(hub.type_text(str(action.get("text", ""))))
        case "press_key":
            hub.actions(hub.press_key(str(action.get("key", "enter"))))
        case "hotkey":
            keys = action.get("keys", "")
            if isinstance(keys, list):
                keys = "+".join(keys)
            hub.actions(hub.hotkey(str(keys)))
        case "scroll_up":
            hub.actions(hub.scroll_up(x, y))
        case "scroll_down":
            hub.actions(hub.scroll_down(x, y))
        case "drag":
            hub.actions(hub.drag(
                int(action.get("x1", 400)), int(action.get("y1", 400)),
                int(action.get("x2", 600)), int(action.get("y2", 600)),
            ))
        case "wait" | _:
            pass


async def _call_agent(
    hub: ModuleType,
    name: str,
    system: str,
    user: str,
) -> str:
    hub.set_agent_status(name, "awaiting_vlm")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    result = await hub.call_vlm_agent(
        messages,
        temperature=0.5,
        max_tokens=512,
        agent_name=name,
    )
    hub.swarm_message(name, "output", result or "(no response)")
    hub.set_agent_status(name, "idle")
    return result or ""


async def _debate_round(
    hub: ModuleType,
    state: dict,
    observation: str,
    prior_debate: str,
) -> str:
    ctx = {
        "goal": state["goal"],
        "phase": state["phase"],
        "progress": state["progress"],
        "observation": observation,
        "history": state["history"],
        "debate": prior_debate,
    }
    tasks = [
        asyncio.create_task(
            _call_agent(
                hub,
                name,
                _AGENT_SYSTEM[name].format(**ctx),
                f"Goal: {state['goal']}\nScreen: {observation}",
            )
        )
        for name in _AGENTS
    ]
    results = await asyncio.gather(*tasks)
    return "\n".join(
        f"[{name}]: {text}"
        for name, text in zip(_AGENTS.keys(), results)
    )


async def main(hub: ModuleType) -> None:
    state: dict = {
        "goal": CURRENT_TASK,
        "phase": "INTERPRET",
        "progress": 0,
        "history": "Starting.",
    }

    for name in _AGENTS:
        hub.set_agent_status(name, "idle")
    hub.set_agent_status("Grok", "idle")

    while True:
        hub.log_event(f"Cycle start — phase={state['phase']} progress={state['progress']}%")

        frame_b64 = await hub.get_frame()
        hub.request_fresh_frame()

        for ov in _make_progress_overlay(state["phase"], state["progress"], state["goal"]):
            hub.overlays(ov)

        observation = f"Phase={state['phase']} Progress={state['progress']}% Goal={state['goal']}"

        debate = ""
        for round_num in range(3):
            hub.log_event(f"Debate round {round_num + 1}")
            debate = await _debate_round(hub, state, observation, debate)

        hub.set_agent_status("Grok", "thinking")
        captain_system = _CAPTAIN_SYSTEM.format(
            goal=state["goal"],
            phase=state["phase"],
            progress=state["progress"],
        )
        captain_messages = [
            {"role": "system", "content": captain_system},
            {
                "role": "user",
                "content": (
                    f"Screen observation: {observation}\n"
                    f"Full agent debate:\n{debate}\n"
                    "Output ONLY the JSON decision now."
                ),
            },
        ]
        hub.swarm_message(
            "Grok",
            "input",
            f"Synthesizing {len(debate)} chars of debate",
            image_b64=frame_b64,
            system=captain_system,
        )

        raw = await hub.call_vlm_orchestrator(
            captain_messages,
            temperature=0.2,
            max_tokens=800,
            agent_name="Grok",
        )

        hub.swarm_message("Grok", "output", raw or "(no response)")
        hub.set_agent_status("Grok", "idle")

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            decision = json.loads(raw[start:end]) if start >= 0 and end > start else {}
        except (json.JSONDecodeError, ValueError):
            hub.log_event("Captain JSON parse failed", "warn")
            decision = {}

        actions: list = decision.get("actions", [])
        is_complete: bool = bool(decision.get("is_complete", False))
        next_goal: str | None = decision.get("next_goal")
        new_progress: int = int(decision.get("progress", state["progress"]))

        hub.set_agent_status("Grok", "acting")
        for act in actions:
            _dispatch_action(hub, act)
            await asyncio.sleep(hub.cfg("action_delay_seconds", 0.15))
        hub.set_agent_status("Grok", "idle")

        if is_complete and next_goal:
            state["history"] = f"Completed: {state['goal']}. Now: {next_goal}"
            state["goal"] = next_goal
            state["phase"] = "INTERPRET"
            state["progress"] = 0
            hub.log_event(f"Goal complete, transitioning to: {next_goal}", "ok")
        else:
            state["progress"] = new_progress
            state["phase"] = "EXECUTE" if actions else "EVALUATE"

        await asyncio.sleep(0.5)
