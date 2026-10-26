"""Frozen Windows companion entry point."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import sys
import traceback


def _command_log_path() -> Path:
    """Keep headless command diagnostics outside the game installation."""
    from wimna.live import documents_root

    return documents_root() / "WIMNA Test Director" / "logs" / "companion-command.log"


def _invoke_command(name: str, argv: list[str]) -> int:
    if name == "--generate-presets":
        from wimna.cm_presets import main as command_main

        command_argv = argv
    elif name == "--prepare-overnight":
        from wimna.overnight_prepare import main as command_main

        command_argv = argv
    elif name == "--overnight-runner":
        from wimna.overnight_live import main as command_main

        # The packaged spelling makes the action explicit without requiring a
        # second, redundant `run` word from a Windows shortcut or script.
        command_argv = ["run", *argv]
    else:
        raise ValueError(f"unknown packaged command: {name}")
    return int(command_main(command_argv))


def _run_command(name: str, argv: list[str]) -> int:

    # PyInstaller's --windowed executable has no attached stdout/stderr. A
    # command failure still needs a durable local explanation and exit code.
    if sys.stdout is not None and sys.stderr is not None:
        return _invoke_command(name, argv)
    log_path = _command_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(f"\n{datetime.now(timezone.utc).isoformat()} {name}")
            try:
                return _invoke_command(name, argv)
            except Exception:
                traceback.print_exc(file=log)
                return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--generate-presets", "--prepare-overnight", "--overnight-runner"):
        return _run_command(args[0], args[1:])
    if "--gui-self-test" in args:
        try:
            import tkinter

            interpreter = tkinter.Tcl()
            interpreter.eval("info patchlevel")
            window = tkinter.Tk()
            window.withdraw()
            window.destroy()
        except Exception:
            import os
            import traceback
            from pathlib import Path

            diagnostic_path = os.environ.get("WIMNA_GUI_SELF_TEST_LOG")
            if diagnostic_path:
                detail = traceback.format_exc()
                bundle_root = Path(getattr(sys, "_MEIPASS", ""))
                if bundle_root:
                    init_file = bundle_root / "_tcl_data" / "init.tcl"
                    detail += f"\nTCL_LIBRARY={os.environ.get('TCL_LIBRARY')!r}; TK_LIBRARY={os.environ.get('TK_LIBRARY')!r}\n"
                    detail += f"Bundle root={bundle_root}; init.tcl exists={init_file.is_file()}\n"
                    detail += f"Tcl data entries={sorted(item.name for item in init_file.parent.iterdir())[:25]!r}\n"
                    detail += f"Tcl encoding directory exists={(init_file.parent / 'encoding').is_dir()}\n"
                    try:
                        import ctypes

                        tcl = ctypes.CDLL(str(bundle_root / "tcl86t.dll"))
                        tcl.Tcl_CreateInterp.restype = ctypes.c_void_p
                        tcl.Tcl_Init.argtypes = [ctypes.c_void_p]
                        tcl.Tcl_Init.restype = ctypes.c_int
                        tcl.Tcl_GetStringResult.argtypes = [ctypes.c_void_p]
                        tcl.Tcl_GetStringResult.restype = ctypes.c_char_p
                        tcl.Tcl_GetVar.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
                        tcl.Tcl_GetVar.restype = ctypes.c_char_p
                        tcl.Tcl_SetVar.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                        tcl.Tcl_SetVar.restype = ctypes.c_char_p
                        tcl.Tcl_EvalFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                        tcl.Tcl_EvalFile.restype = ctypes.c_int
                        tcl.Tcl_Eval.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                        tcl.Tcl_Eval.restype = ctypes.c_int
                        tcl.Tcl_DeleteInterp.argtypes = [ctypes.c_void_p]
                        pointer = tcl.Tcl_CreateInterp()
                        tcl_path = init_file.as_posix().encode()
                        tcl.Tcl_SetVar(pointer, b"tcl_library", init_file.parent.as_posix().encode(), 1)
                        tcl.Tcl_Eval(pointer, b"file exists {" + tcl_path + b"}")
                        detail += f"Tcl file exists={tcl.Tcl_GetStringResult(pointer)!r}\n"
                        eval_status = tcl.Tcl_EvalFile(pointer, tcl_path)
                        eval_result = tcl.Tcl_GetStringResult(pointer)
                        eval_error_info = tcl.Tcl_GetVar(pointer, b"errorInfo", 1)
                        detail += f"Manual init.tcl status={eval_status}; result={eval_result!r}; errorInfo={eval_error_info!r}\n"
                        status = tcl.Tcl_Init(pointer)
                        result = tcl.Tcl_GetStringResult(pointer)
                        error_info = tcl.Tcl_GetVar(pointer, b"errorInfo", 1)
                        detail += f"Tcl_Init status={status}; result={result!r}; errorInfo={error_info!r}\n"
                        tcl.Tcl_DeleteInterp(pointer)
                    except Exception:
                        detail += traceback.format_exc()
                Path(diagnostic_path).write_text(detail, encoding="utf-8")
            return 1
        return 0
    if "--self-test" in args:
        from wimna.gui import _template_config
        from wimna.cm_presets import build_campaign_pairs
        from wimna.models import thermal_assumption_profiles
        from wimna.overnight_live import build_live_runner
        from wimna.overnight_prepare import prepare_campaign
        from wimna.scheduler import CampaignScheduler
        CampaignScheduler(_template_config())
        thermal_assumption_profiles()
        if not all(callable(item) for item in
                   (build_campaign_pairs, build_live_runner, prepare_campaign)):
            return 1
        return 0
    from wimna.gui import main as gui_main
    return int(gui_main())


if __name__ == "__main__":
    raise SystemExit(main())
