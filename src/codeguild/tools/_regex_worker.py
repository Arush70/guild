"""Run one regex over a list of files, as its own process.

Python cannot interrupt a regular expression. A pattern such as `(a+)+$` can take minutes on
one line, and the thread running it cannot be stopped. The `search` tool therefore runs the
matching here and kills this process when it takes too long.

Standalone on purpose (standard library only, started by file path): starting it must not
cost the import of the whole package for every search.

stdin:  {"pattern": str, "files": [[shown_name, path], ...], "max": int}
stdout: {"hits": [str, ...], "truncated": bool}
"""

import json
import re
import sys


def main() -> None:
    job = json.load(sys.stdin)
    rx = re.compile(job["pattern"])
    limit = job["max"]
    hits: list[str] = []
    for shown, path in job["files"]:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for number, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{shown}:{number}: {line.rstrip()[:200]}")
                        if len(hits) >= limit:
                            json.dump({"hits": hits, "truncated": True}, sys.stdout)
                            return
        except OSError:
            continue
    json.dump({"hits": hits, "truncated": False}, sys.stdout)


if __name__ == "__main__":
    main()
