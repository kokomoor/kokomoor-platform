# AGENTS.md — core/web_agent/

LLM-driven web navigation controller. Sits on top of `core/browser/` (hands + eyes)
and uses `core/llm/structured_complete` (brain) to drive a Playwright page toward a
declared goal.

## Architecture

```
PageObserver (core/browser/observer.py)  →  PageState
    ↓
AgentContextManager (context.py)  →  prompt text
    ↓
structured_complete (core/llm/)  →  AgentAction (Pydantic)
    ↓
WebAgentController (controller.py)  →  BrowserActions (core/browser/actions.py)
    ↓
AgentStep recorded  →  loop or terminate
```

## Key models (protocol.py)

| Model | Purpose |
|-------|---------|
| `AgentAction` | Single LLM decision: action verb + element index + value |
| `AgentStep` | One completed observe-act cycle |
| `AgentGoal` | Declarative task spec: instruction, signals, approval gates |
| `AgentResult` | Final outcome: status, steps taken, history |

## Rules

- **Never auto-submit.** Any action listed in `goal.require_human_approval_before`
  pauses the loop and returns `AgentResult(status="awaiting_approval")`.
- **DOM over screenshots.** The agent uses structured `PageState` (forms, elements,
  text) rather than vision-model screenshot analysis. This is cheaper, faster,
  and stealthier.
- **No pipeline logic here.** Job-specific prompts, QA answerers, and workflow
  orchestration belong in `pipelines/`. This package provides the generic loop.
- **History compression is mandatory.** `AgentContextManager` keeps the last N steps
  verbatim and compresses older steps to one-liners. Without this, multi-step
  workflows exceed context limits.
- **`structured_complete` is the only LLM interface.** The controller does not use
  native tool calling. It gets a `PageState`, asks for an `AgentAction`, executes
  it. This keeps the LLM interface simple and provider-agnostic.
