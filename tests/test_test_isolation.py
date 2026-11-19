"""The test run must never write into the folder of a real install."""
import os
import tempfile

from callscoot import config, errors


def test_tests_run_in_a_throwaway_home():
    home = config.app_home()
    assert os.path.realpath(home) != os.path.realpath(config.REPO_DIR)
    assert os.path.basename(home).startswith("callscoot-test-home-")


def test_error_log_lands_in_the_throwaway_home():
    log = errors.setup()
    paths = [h.baseFilename for h in log.handlers if hasattr(h, "baseFilename")]
    assert paths
    home = os.path.realpath(config.app_home())
    for p in paths:
        assert not os.path.realpath(p).startswith(os.path.realpath(config.REPO_DIR) + os.sep)
    # setup() sticks to the first folder it gets; a test that passed its own
    # logs.dir first is fine, as long as it is not the program folder.
    tmp = os.path.realpath(tempfile.gettempdir())
    assert any(os.path.realpath(p).startswith((home, tmp)) for p in paths)
