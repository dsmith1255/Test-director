"""Frozen Windows companion entry point."""
from __future__ import annotations

import sys


def main() -> int:
    if "--gui-self-test" in sys.argv:
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
    if "--self-test" in sys.argv:
        from wimna.gui import _template_config
        from wimna.models import thermal_assumption_profiles
        from wimna.scheduler import CampaignScheduler
        CampaignScheduler(_template_config())
        thermal_assumption_profiles()
        return 0
    from wimna.gui import main as gui_main
    return int(gui_main())


if __name__ == "__main__":
    raise SystemExit(main())
