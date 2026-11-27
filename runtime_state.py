"""Sdílené stavové a diagnostické typy používané automatizací Fishao."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from typing import Any


class BotState(str, Enum):
    CHECKING_ENV = "CHECKING_ENV"
    READY = "READY"
    CASTING = "CASTING"
    WAITING_BITE = "WAITING_BITE"
    MINIGAME = "MINIGAME"
    HANDLING_RESULT = "HANDLING_RESULT"
    HANDLING_LEVEL = "HANDLING_LEVEL"
    ENERGY_WAIT = "ENERGY_WAIT"
    REFILL = "REFILL"
    GEAR_SELECT = "GEAR_SELECT"
    MISSION_WORK = "MISSION_WORK"
    PAUSED_MANUAL = "PAUSED_MANUAL"
    PAUSED_LOCKED = "PAUSED_LOCKED"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
    ERROR_STOPPED = "ERROR_STOPPED"


class StateMachine:
    def __init__(self) -> None:
        self.state = BotState.CHECKING_ENV
        self.history: list[tuple[float, BotState, dict[str, Any]]] = []

    def transition(self, state: BotState, now: float, **details: Any) -> None:
        self.state = state
        self.history.append((now, state, details))
        log_structured(state=state, action=details.get("action"), result=details.get("result"),
                       retry=details.get("retry"), reason=details.get("reason"),
                       level=details.get("level", logging.INFO))

    def record_progress(self) -> None:
        log_structured(state=self.state, action="progress", result="confirmed")

    def record_recovery(self, now: float, **details: Any) -> None:
        self.transition(BotState.RECOVERING, now, **details)

    def reset_after_cooldown(self, now: float, reason: str = "") -> None:
        self.transition(BotState.READY, now, action="cooldown_done", result="ready", reason=reason)


class EventFeed:
    def __init__(self, logger_name: str = "fishao.events") -> None:
        self.logger = logging.getLogger(logger_name)
        self._last: dict[str, tuple[float, str]] = {}

    def emit(self, message: str, *, key: str = "", cooldown_s: float = 0.0,
             now: float | None = None, level: int = logging.INFO) -> None:
        stamp = time.perf_counter() if now is None else now
        previous = self._last.get(key)
        if key and previous and previous[1] == message and stamp - previous[0] < cooldown_s:
            return
        if key:
            self._last[key] = (stamp, message)
        self.logger.log(level, "%s", message)


class MissionStatus(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    OFFERED = "offered"
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"


@dataclass(slots=True)
class MissionRecord:
    signature: str
    goal: str = ""
    status: MissionStatus = MissionStatus.UNKNOWN
    progress: str = ""


class MissionTracker:
    def __init__(self) -> None:
        self.records: dict[str, MissionRecord] = {}
        self.pending_accept_sig: str | None = None
        self.pending_claim_sig: str | None = None

    def observe(self, signature: str, *, goal: str = "", now: float, has_offer: bool,
                has_claim: bool, progress: str = "", supported: bool = True) -> MissionRecord:
        record = self.records.setdefault(signature, MissionRecord(signature, goal))
        record.goal = goal or record.goal
        record.progress = progress
        if not supported:
            record.status = MissionStatus.UNSUPPORTED
        elif has_claim:
            record.status = MissionStatus.COMPLETED
        elif has_offer:
            record.status = MissionStatus.OFFERED
        else:
            record.status = MissionStatus.ACTIVE
        return record

    def should_accept(self, signature: str, now: float) -> bool:
        return self.pending_accept_sig != signature

    def should_claim(self, signature: str, now: float) -> bool:
        return self.pending_claim_sig != signature

    def mark_accept_sent(self, signature: str, now: float) -> None:
        self.pending_accept_sig = signature

    def mark_claim_sent(self, signature: str, now: float) -> None:
        self.pending_claim_sig = signature

    def confirm_accept(self, signature: str) -> bool:
        if self.pending_accept_sig != signature:
            return False
        self.pending_accept_sig = None
        if signature in self.records:
            self.records[signature].status = MissionStatus.ACTIVE
        return True

    def verify_claim_completed(self, now: float, signature: str) -> bool:
        if self.pending_claim_sig != signature:
            return False
        self.pending_claim_sig = None
        if signature in self.records:
            self.records[signature].status = MissionStatus.COMPLETED
        return True

    def fail_pending(self, reason: str) -> None:
        self.pending_accept_sig = None
        self.pending_claim_sig = None

    def has_active_mission(self) -> bool:
        return any(
            record.status in (MissionStatus.ACTIVE, MissionStatus.OFFERED, MissionStatus.COMPLETED)
            for record in self.records.values()
        )


class LevelDialogController:
    """Řídí dvě ověřovací snímky, jedno kliknutí a záložní Escape."""
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.button = None
        self.clicks = 0
        self.escape_sent = False
        self.click_after = 0.0
        self._visible_since: float | None = None

    def step(self, button: Any, now: float) -> str:
        if button is None:
            if self.clicks > 0 or self.escape_sent:
                self.button = None
                return "none"
            self._visible_since = None
            return "none"
        self.button = button
        if self.clicks == 0 and not self.escape_sent:
            return "click"
        if now - self.click_after >= 2.0 and not self.escape_sent:
            return "escape"
        if now - self.click_after >= 8.0:
            return "timeout"
        return "waiting"

    def mark_clicked(self, now: float) -> None:
        self.clicks += 1
        self.click_after = now

    def mark_escaped(self, now: float) -> None:
        self.escape_sent = True
        self.click_after = now


def classify_mission_text(text: str) -> str:
    low = (text or "").casefold()
    if any(word in low for word in ("claim", "collect", "reward", "odměn", "vyzved")):
        return "claim"
    if any(word in low for word in ("accept", "přijmout", "accept mission")):
        return "offer"
    return "unknown"


def log_structured(*, state: BotState | str, action: str | None = None,
                   result: str | None = None, retry: int | None = None,
                   reason: str | None = None, level: int = logging.INFO) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    value = getattr(state, "value", str(state))
    message = f"ts={ts} | state={value}"
    if action is not None:
        message += f" | action={action}"
    if result is not None:
        message += f" | result={result}"
    if retry is not None:
        message += f" | retry={retry}"
    if reason:
        message += f" | reason={reason}"
    logging.getLogger("fishao").log(level, "%s", message)