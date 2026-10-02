"""Thread-safe command bus shared by the Telegram control listener, the web
GUI, and the live call session.

The bus is intentionally tiny: one Queue of queued actions the session loop
drains between turns, and a lock-guarded state dict the control surfaces read.
Anything that must act mid-call (record arm, copilot toggle, hangup, spoken
text) goes through here — never through shared mutable globals scattered
across modules.
"""
import queue
import threading


# Commands the session loop understands.
REC_ON = "rec_on"
REC_OFF = "rec_off"
AI_TAKEOVER = "ai_takeover"      # in copilot mode: resume autonomous replies
AI_DROP = "ai_drop"              # in autonomous mode: stop speaking, keep transcribing
HANGUP = "hangup"
SAY_TO_CALLER = "say_to_caller"  # payload = text, spoken into the call verbatim


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
        """Start or stop the bot talking. Separate from recording."""
        with self._lock:
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
                "number": self.call_number,
                "started": self.session_started,
                "last_event": self.last_event,
                "link": self.link_status,
            }
