"""Email the user when an automatic betting run hits a problem, so nobody has to watch the runs.
Usage: alert.py "<what happened>" [log file]   (sends the last 40 lines of the log)"""

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nfai import report  # noqa: E402


def main():
    what = sys.argv[1]
    log = Path(sys.argv[2]).read_text(errors="replace").splitlines()[-40:] if len(sys.argv) > 2 and Path(sys.argv[2]).exists() else []
    body = (f"<p><b>{html.escape(what)}</b></p><p>The automatic betting run on GitHub reported a problem. "
            "Bets for this wave may not have been placed.</p>"
            f"<pre style='font-size:12px;white-space:pre-wrap'>{html.escape(chr(10).join(log))}</pre>")
    report.send(f"NFAI alert: {what}", body)
    print(f"alert emailed: {what}")


if __name__ == "__main__":
    main()
