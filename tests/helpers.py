import os
import shutil

def get_bash_cmd() -> str:
    """Resolve the bash executable across platforms (Linux, macOS, Windows Git Bash)."""
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return "bash"
