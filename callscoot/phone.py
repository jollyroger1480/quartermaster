"""ADB control path: call state, answer, hangup, dial, unlock."""
import os
import re
import subprocess
import time

from .config import app_home, dig


class PhoneError(RuntimeError):
    pass


def adb(args, timeout=15):
    p = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def ensure_connected(cfg):
    out, _, rc = adb(["get-state"])
    if rc == 0 and out.strip() == "device":
        return
    ip = dig(cfg, "phone.adb_ip", "")
    port = dig(cfg, "phone.adb_port", 5555)
    if ip:
        adb(["connect", f"{ip}:{port}"], timeout=12)
        out, _, rc = adb(["get-state"])
        if rc == 0 and out.strip() == "device":
            return
    raise PhoneError(
        "No ADB device. Run `callscoot adb-connect` (or pair wireless debugging — see README)."
    )


def call_state():
    """0=idle 1=ringing(incoming) 2=offhook."""
    out, err, rc = adb(["shell", "dumpsys", "telephony.registry"])
    if rc != 0:
        raise PhoneError(f"dumpsys failed: {err or out}")
    state, num = None, None
    m = re.search(r"mCallState=(\d)", out)
    if m:
        state = int(m.group(1))
    m = re.search(r"mCallIncomingNumber=(?:[^:]*:)?([+\d][^\s,]*)", out)
    if m:
        num = m.group(1).strip()
    return {"state": state, "number": num}


def answer(cfg=None):
    codes = dig(cfg, "phone.answer_keycodes", None) or [
        "KEYCODE_HEADSETHOOK", "KEYCODE_CALL",
    ]
    for code in codes:
        adb(["shell", "input", "keyevent", code])
        time.sleep(0.8)
        if call_state()["state"] == 2:
            return code
    raise PhoneError(
        "Could not answer while locked (OEM-dependent). Keep screen on while plugged in, "
        "or answer via `scrcpy` once and retry."
    )


def hangup():
    for _ in range(3):
        adb(["shell", "input", "keyevent", "KEYCODE_ENDCALL"])
        time.sleep(0.7)
        if call_state()["state"] != 2:
            return
    raise PhoneError("call still offhook after KEYCODE_ENDCALL x3")


def dial(cfg, number):
    out, err, rc = adb(
        ["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}"],
        timeout=20,
    )
    if rc != 0:
        raise PhoneError(f"dial failed: {err or out}")


def last10(number):
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


def allowlisted(cfg, number):
    entries = dig(cfg, "phone.allowlist", []) or []
    allowed = {last10(e) for e in entries if e}
    return last10(number) in allowed


PIN_FILE = os.path.join(app_home(), ".pin")


def keyguard_showing():
    out, _, _ = adb(["shell", "dumpsys", "window", "policy"])
    m = re.search(r"showing=(\w+)", out)
    return (m.group(1).lower() == "true") if m else None


def unlock(cfg=None):
    """Wake + unlock the phone using the saved PIN (for SMS UI automation).
    Returns True when the keyguard is no longer showing."""
    adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    time.sleep(1)
    if keyguard_showing() is False:
        return True
    if not os.path.isfile(PIN_FILE):
        raise PhoneError("no PIN saved — run: read -s -p 'PIN: ' P && printf '%s' \"$P\" > "
                         + PIN_FILE + " && chmod 600 " + PIN_FILE)
    pin = open(PIN_FILE).read().strip()
    if not pin.isdigit():
        raise PhoneError("saved PIN is not numeric — refusing to type it")
    adb(["shell", "input", "swipe", "500", "1500", "500", "300"])
    time.sleep(1.5)
    adb(["shell", "input", "text", pin])
    time.sleep(0.5)
    adb(["shell", "input", "keyevent", "66"])
    time.sleep(2)
    unlocked = keyguard_showing() is False
    if not unlocked:
        # fallback: the PIN pad takes taps, not text — press each digit key
        for d in pin:
            adb(["shell", "input", "text", d])
            time.sleep(0.15)
        adb(["shell", "input", "keyevent", "66"])
        time.sleep(2)
        unlocked = keyguard_showing() is False
    if not unlocked:
        raise PhoneError("unlock failed — PIN wrong or lockscreen layout unexpected")
    return True


def lock_screen():
    adb(["shell", "input", "keyevent", "KEYCODE_SLEEP"])
