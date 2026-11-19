"""Keep the test run away from a real install.

callscoot keeps its logs, flags and state under app_home() (CALLSCOOT_HOME,
or the program folder). errors.setup() attaches its log file once, to the
first logs folder it is given, so without this a test run wrote its panel
clicks ("secretary off" / "secretary on" in the same second) and fake
playback lines into the live errors.log next to a running watcher.
Point CALLSCOOT_HOME at a throwaway folder before any callscoot import.
"""
import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="callscoot-test-home-")
os.environ["CALLSCOOT_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
