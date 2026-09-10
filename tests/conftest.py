import os
import shutil
import subprocess
import builtins
from tests.helpers import get_bash_cmd

_ORIG_POPEN = subprocess.Popen

class WindowsBashPopen(_ORIG_POPEN):
    def __init__(self, args, *nargs, **kwargs):
        if os.name == "nt":
            bash_bin = get_bash_cmd()
            if isinstance(args, (list, tuple)) and len(args) > 0:
                new_args = list(args)
                first_cmd = str(new_args[0])
                if first_cmd == "bash":
                    new_args[0] = bash_bin
                elif first_cmd.endswith(".sh") or (not os.path.splitext(first_cmd)[1] and not shutil.which(first_cmd)):
                    new_args = [bash_bin] + new_args
                args = new_args

            if kwargs.get("text") or kwargs.get("universal_newlines"):
                if not kwargs.get("encoding"):
                    kwargs["encoding"] = "utf-8"
                if not kwargs.get("errors"):
                    kwargs["errors"] = "replace"

            # On Windows MSYS, files without executable permissions in temp directories are ignored by which/PATH
            env = kwargs.get("env")
            if env and "PATH" in env:
                first_path = env["PATH"].split(os.pathsep)[0]
                if first_path and os.path.isdir(first_path) and ("snack_test" in first_path or "Temp" in first_path or "tmp" in first_path):
                    for fname in os.listdir(first_path):
                        fpath = os.path.join(first_path, fname).replace("\\", "/")
                        _ORIG_POPEN([bash_bin, "-c", f"chmod +x '{fpath}'"]).wait()

                    posix_dir = first_path.replace("\\", "/")
                    if len(posix_dir) >= 3 and posix_dir[1:3] == ":/":
                        posix_dir = "/" + posix_dir[0].lower() + posix_dir[2:]

                    if isinstance(args, (list, tuple)) and len(args) >= 2 and args[0] == bash_bin:
                        if args[1] == "-c":
                            args = list(args)
                            args[2] = f'export PATH="{posix_dir}:$PATH"; {args[2]}'
                        else:
                            script_args = list(args[1:])
                            script_args_str = " ".join([f'"{a.replace(os.sep, "/")}"' for a in script_args])
                            args = [bash_bin, "-c", f'export PATH="{posix_dir}:$PATH"; exec bash {script_args_str}']

        super().__init__(args, *nargs, **kwargs)

if os.name == "nt":
    subprocess.Popen = WindowsBashPopen

    _orig_open = builtins.open
    def utf8_open(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
            if "errors" not in kwargs:
                kwargs["errors"] = "replace"
        return _orig_open(file, *args, **kwargs)

    builtins.open = utf8_open
