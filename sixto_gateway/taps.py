"""Who owns a button tap (ticket #50).

A tap whose callback data carries a module prefix (``mod:<module>:<payload>``, set by
``/notify``) is delivered to that module once, as a ``tap`` item. Every other tap is
onboarding's: the existing handler saves it for the onboarding worker, unchanged.
"""

from __future__ import annotations

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision, parse_module_callback
from sixto_gateway.onboarding import decide_tap as decide_onboarding_tap
from sixto_gateway.telegram.normalize import IncomingUpdate


def decide_tap(config: Config, update: IncomingUpdate) -> Decision:
    """Route a tap to its module, or leave it to onboarding."""
    parsed = parse_module_callback(update.callback_data)
    if parsed is None:
        return decide_onboarding_tap(update)
    module, _ = parsed
    if module not in config.modules:
        # Answered so the spinner stops; stored but never delivered.
        return Decision(
            None,
            audit=("tap_unknown_module", None, f"module={module} user={update.from_id}"),
            answer_tap=strings.TAP_UNKNOWN_MODULE,
        )
    return Decision(
        module,
        kind="tap",
        push=config.modules[module].delivery == "push",
        answer_tap="",
    )
