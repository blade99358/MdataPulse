"""Site settings shared by the tools, read from the environment (/root/.zabbix_env or mdatapulse.conf).

  SITE_CODE_PREFIX   letters in front of the 4-digit site code (default SITE: SITE0000, SITE0015 ...)
  CORE_SITE          site where the network starts (default <prefix>0000)
  MDP_DATA_DIR       folder with the private data files (default: data/ next to tools/)

Private data (device lists, locations, names) never lives in the code: it is read from MDP_DATA_DIR.
"""
import os
import re

SITE_CODE_PREFIX = (os.environ.get("SITE_CODE_PREFIX") or "SITE").upper()
P = re.escape(SITE_CODE_PREFIX)
CODE_RE = re.compile(P + r"\d{4}")
CORE_SITE = os.environ.get("CORE_SITE") or SITE_CODE_PREFIX + "0000"
DATA_DIR = os.environ.get("MDP_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def code(n):
    """5 -> SITE0005"""
    return f"{SITE_CODE_PREFIX}{int(n):04d}"


def data_file(name):
    """Text of a private data file, or '' when it is not there."""
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
