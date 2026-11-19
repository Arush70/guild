"""`python -m codeguild …` works like the `codeguild` command (useful when the Scripts folder
with codeguild.exe is not on PATH)."""

from .cli import app

app(prog_name="codeguild")
