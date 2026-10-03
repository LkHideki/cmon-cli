import subprocess
import sys
import time

import cmon

HOLD = """
import sys, time, duckdb
con = duckdb.connect(sys.argv[1])
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _hold(path, seconds):
    p = subprocess.Popen([sys.executable, "-c", HOLD, path, str(seconds)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


def test_db_retry_gives_up_while_another_process_writes_then_recovers(home):
    cmon.db().close()
    holder = _hold(cmon.DB, 1.5)
    try:
        t = time.monotonic()
        assert cmon._db_retry(0.3) is None
        assert time.monotonic() - t < 1.5
        con = cmon._db_retry(5)  # waits the holder out
        assert con is not None
        con.close()
    finally:
        holder.wait()
