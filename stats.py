from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Stats:
    attempts: int = 0
    caught: int = 0
    escaped: int = 0

    def snapshot(self, active_s: float) -> dict[str, str]:
        completed = self.caught + self.escaped
        seconds = int(active_s)
        return {
            "Pokusy": str(self.attempts),
            "Chyceno": str(self.caught),
            "Uteklo": str(self.escaped),
            "Úspěšnost": f"{100 * self.caught / completed:.1f} %" if completed else "—",
            "Ryb za hodinu": f"{3600 * self.caught / active_s:.1f}" if active_s > 0 else "—",
            "Čas běhu": f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}",
        }

