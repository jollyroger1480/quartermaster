"""Thread-safe command bus shared by the Telegram control listener, the web
GUI, and the live call session.

The bus is intentionally tiny: one Queue of queued actions the session loop
drains between turns, and a lock-guarded state dict the control surfaces read.
Anything that must act mid-call (record arm, copilot toggle, hangup, spoken
text) goes through here — never through shared mutable globals scattered
across modules.
"""
import os
import queue
import threading

from .config import dig


# Commands the session loop understands.
REC_ON = "rec_on"
REC_OFF = "rec_off"
AI_TAKEOVER = "ai_takeover"      # in copilot mode: resume autonomous replies
AI_DROP = "ai_drop"              # in autonomous mode: stop speaking, keep transcribing
HANGUP = "hangup"
SAY_TO_CALLER = "say_to_caller"  # payload = text, spoken into the call verbatim


RINGS_MIN = 1
RINGS_MAX = 8


def _logs_dir(cfg):
    return os.path.expanduser(dig(cfg or {}, "logs.dir", "") or "")


def secretary_flag_path(cfg):
    """Off-switch file under logs. Absent means the secretary is on."""
    d = _logs_dir(cfg)
    if not d:
        return None
    return os.path.join(d, "secretary.off")


def rings_path(cfg):
    """Saved ring count. Absent means call.rings, or 1."""
    d = _logs_dir(cfg)
    if not d:
        return None
    return os.path.join(d, "rings")


def clamp_rings(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = RINGS_MIN
    return max(RINGS_MIN, min(RINGS_MAX, n))


class CallControls:
    """One live instance per process, shared by every control surface."""

    def __init__(self):
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self.on_call = False           # a session (autonomous or copilot) is running
        self.copilot = False           # bot silent, transcribe-only
        self.recording = False
        self.pending_record = False    # record the next offhook call, do not start the bot
        self.pending_join = False      # AI join requested while no session runs
        self.secretary = True          # watcher still answers new rings and the bot talks
        self.rings = RINGS_MIN          # how many ring_delay_s waits before answer
        self.ring_seconds = 3.0
        self.call_number = "unknown"
        self.session_started = 0.0
        self.last_event = ""
        self.link_status = ""

    # ── producers (Telegram poller, web GUI, CLI) ────────────────────────
    def post(self, command, payload=None):
        self._q.put((command, payload))

    def note_event(self, text):
        with self._lock:
            self.last_event = text

    def toggle_record(self):
        """Arm or stop recording. Off-call this does not start the bot."""
        with self._lock:
            recording = self.recording
            pending = self.pending_record
        if self.on_call:
            self.post(REC_OFF if recording else REC_ON)
            return "recording stopped" if recording else "recording started"
        if pending:
            self.pending_record = False
            self.post(REC_OFF)
            self.note_event("recording cancelled")
            return "recording cancelled"
        self.pending_record = True
        self.post(REC_ON)
        self.note_event("record armed — AI stays off")
        return "record armed, AI off"

    def toggle_ai(self):
        """Join or quiet the bot on a call already up. Does not stop the secretary."""
        with self._lock:
            if not self.secretary:
                self.last_event = "secretary is off"
                return "secretary is off. Turn secretary on before the bot can talk."
            on_call, copilot = self.on_call, self.copilot
        if not on_call:
            if self.pending_join:
                self.pending_join = False
                self.note_event("AI cancelled")
                return "AI cancelled"
            self.pending_join = True
            self.note_event("AI will take the live call and talk")
            return "AI will join and talk"
        self.post(AI_TAKEOVER if copilot else AI_DROP)
        return "bot took over the call" if copilot else "bot stepped back (copilot)"

    def load_secretary(self, flag_path):
        """A leftover off-flag means the secretary stays off across a restart."""
        if flag_path and os.path.exists(flag_path):
            with self._lock:
                self.secretary = False

    def toggle_secretary(self, flag_path=None):
        """Stop or resume auto-answer. The panel stays up. A live line is not hung up."""
        with self._lock:
            self.secretary = not self.secretary
            enabled = self.secretary
            quiet_bot = (not enabled) and self.on_call and not self.copilot
            if not enabled:
                self.pending_join = False
                if quiet_bot:
                    self.copilot = True
        if flag_path:
            try:
                if enabled:
                    os.remove(flag_path)
                else:
                    os.makedirs(os.path.dirname(flag_path), exist_ok=True)
                    with open(flag_path, "w", encoding="utf-8") as fh:
                        fh.write("off\n")
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if quiet_bot:
            self.post(AI_DROP)
            msg = "secretary off. Bot is quiet on this call. The line stays up. New rings are not answered."
        elif not enabled:
            msg = "secretary off. New rings are not answered."
        else:
            msg = "secretary on. New rings are answered and the bot talks."
        self.note_event(msg)
        return msg

    def load_rings(self, path, default=RINGS_MIN):
        n = clamp_rings(default)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    n = clamp_rings(fh.read().strip())
            except OSError:
                n = clamp_rings(default)
        with self._lock:
            self.rings = n

    def set_rings(self, n, path=None):
        """How many configured ring-lengths to wait. Does not arm the bot."""
        n = clamp_rings(n)
        with self._lock:
            self.rings = n
            per = self.ring_seconds
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(f"{n}\n")
            except OSError:
                pass
        word = "ring" if n == 1 else "rings"
        msg = f"picks up after {n} {word}, about {int(round(n * per))} seconds"
        self.note_event(msg)
        return msg

    # ── consumer (the session loop) ──────────────────────────────────────
    def drain(self):
        """All queued (command, payload) pairs right now."""
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    # ── state mirror for the GUI / poller UI ─────────────────────────────
    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self):
        with self._lock:
            return {
                "on_call": self.on_call,
                "copilot": self.copilot,
                "recording": self.recording,
                "pending_record": self.pending_record,
                "pending_join": self.pending_join,
                "secretary": self.secretary,
                "rings": self.rings,
                "ring_seconds": self.ring_seconds,
                "number": self.call_number,
                "started": self.session_started,
                "last_event": self.last_event,
                "link": self.link_status,
            }
