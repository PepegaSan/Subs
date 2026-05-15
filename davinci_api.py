#!/usr/bin/env python3
"""Reference implementation for reliably connecting to DaVinci Resolve Studio.

Drop this file into a new project and call :func:`connect_resolve` — it
enforces every rule that tends to cause silent Resolve-API failures on
Windows. Ships with project-side helpers (:func:`list_render_presets`,
:func:`append_dict_for_subclip`, :func:`apply_project_timeline_settings`,
:func:`render_with_preset`) so a downstream pipeline can stay small.

Pattern summary (why each piece exists)
=======================================

1. **Purge stale env vars before import**
   Resolve sets ``RESOLVE_SCRIPT_API`` / ``RESOLVE_SCRIPT_LIB`` globally.
   If a previous run of a different Resolve edition (Free vs Studio vs Beta)
   left them pointing at a dead DLL, the import fails with a useless
   ``ImportError("Could not locate module dependencies")``. Always pop the
   old values first, then set fresh ones from a multi-edition candidate
   list.

2. **Prefer the edition of the running ``Resolve.exe``**
   With multiple Resolve editions installed side by side the candidate
   list alone can bind the wrong ``fusionscript.dll``. Query the running
   process's full path via PowerShell, derive the matching DLL + modules
   dir from it, fall back to the static list only if no Resolve is up.

3. **Search every install layout, not just Free**
   Blackmagic's stock ``DaVinciResolveScript.py`` hardcodes the Free path.
   Studio / Studio 21 Beta / 21 Beta live elsewhere. ``_first_existing``
   picks whichever one actually exists on disk.

4. **``time.sleep(2)`` before importing ``DaVinciResolveScript``**
   Right after Resolve starts, fusionscript.dll can still be file-locked;
   the import silently fails without the short delay.

5. **Forward slashes for every path handed to the API**
   Backslashes silently break ``ImportMedia`` / render target paths on
   Windows. Convert once at the boundary (:func:`to_forward`).

6. **Initialise COM on every worker thread**
   Resolve's scripting C-module uses COM. Threads that haven't called
   ``CoInitializeEx`` get a silent ``None`` from ``scriptapp('Resolve')``
   even though the main thread works fine — a classic "works in script,
   broken in GUI" trap. Wrap worker-thread interaction in the
   :func:`scripting_thread` context manager.

7. **Connect strategy — try, launch only if needed, poll**
   - Call ``scriptapp("Resolve")`` immediately. On a running Resolve this
     returns in < 1s, so the no-op case stays fast.
   - If it returns ``None`` **and** ``Resolve.exe`` is not in the task list,
     launch Resolve.exe. NEVER Popen a second time on an already-running
     Resolve — it wobbles the scripting socket and breaks the session.
   - Poll ``scriptapp`` every 2s for up to 90s. Each call blocks ~5s during
     boot, so a healthy cold start completes in 2-3 attempts (~10-15s).
   - Emit an actionable "External scripting = Local" hint after the second
     failed poll (~4s) rather than burying it in a 90s timeout.

8. **Scratch-project fallback**
   Cold-launched Resolve lands on the Project Manager screen where
   ``GetCurrentProject()`` returns ``None`` forever. Automation must create
   a scratch project so it can proceed unattended.

Common failure modes the connect loop surfaces (in order of frequency):
    1. ``Preferences → System → General → External scripting using`` is not
       set to ``Local``. Saving alone isn't enough; Resolve must restart.
    2. Worker thread never called CoInitializeEx (see :func:`scripting_thread`).
    3. Privilege mismatch — Resolve started as admin while Python runs as
       user, or vice versa. Windows isolates the scripting socket per
       privilege level; both must run at the same elevation.
    4. Modal dialog open inside Resolve (unsaved-changes, render progress,
       auto-save prompt). These block the scripting server from answering.
    5. DaVinci Resolve **Free** — external scripting is Studio-only. Note:
       on Resolve 21 the ``ProductName`` version resource reports the same
       string ("DaVinci Resolve") for both editions, so we deliberately do
       NOT hard-gate on that. We log it for info and let ``scriptapp`` fail.
    6. No project open — Project Manager screen. Handled automatically by
       the scratch-project fallback when you allow it.

Usage
-----

Single-threaded (CLI script)::

    from davinci_api import connect_resolve, to_forward

    resolve, project, media_pool, root_folder = connect_resolve(
        status_callback=print,   # wire to your UI status bar
        auto_launch=True,
    )
    media_pool.ImportMedia([to_forward(r"C:\\clips\\take01.mp4")])

GUI worker thread (CustomTkinter / PySide / Qt threads)::

    from davinci_api import connect_resolve, scripting_thread

    def worker():
        with scripting_thread():            # COM init / uninit for this thread
            resolve, project, *_ = connect_resolve()
            print(project.GetName())

    threading.Thread(target=worker, daemon=True).start()

Full pipeline example (render preset picker + matching timeline)::

    from davinci_api import (
        connect_resolve, scripting_thread, to_forward,
        cleanup_timelines, list_render_presets,
        apply_project_timeline_settings, render_with_preset,
    )

    with scripting_thread():
        resolve, project, media_pool, root = connect_resolve()

        clips = media_pool.ImportMedia([to_forward(video_path)])
        clip = clips[0]

        # Purge our OWN leftover timelines so Resolve no longer locks
        # the project frame rate. Without this step, SetSetting below
        # silently no-ops and the new timeline inherits the old rate.
        cleanup_timelines(project, media_pool, name_prefix="AutoRun_")

        fps = clip.GetClipProperty("FPS") or "25"
        res = clip.GetClipProperty("Resolution") or "1920x1080"
        apply_project_timeline_settings(project, fps, res)  # pass raw string

        timeline = media_pool.CreateEmptyTimeline(f"AutoRun_{int(time.time())}")
        project.SetCurrentTimeline(timeline)
        media_pool.AppendToTimeline([{"mediaPoolItem": clip}])

        # Render preset dropdown UX: if Resolve is already up, use the
        # live list; otherwise the user can still type the preset name
        # by hand into their combobox (the render helper below validates
        # via its fallback chain).
        presets = list_render_presets(project)
        render_with_preset(
            project,
            output_dir=r"C:\\output",
            output_name="take01_autoedit",
            preset_name=presets[0] if presets else None,
            status_callback=print,
        )

Run this file directly to see a live demo::

    python davinci_api.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Install-location candidate lists
# ---------------------------------------------------------------------------

# Where ``DaVinciResolveScript.py`` can live, across editions.
_RESOLVE_MODULE_DIRS: Tuple[str, ...] = (
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve Studio\Support\Developer\Scripting\Modules",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve Studio 21 Beta\Support\Developer\Scripting\Modules",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve 21 Beta\Support\Developer\Scripting\Modules",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\Support\Developer\Scripting\Modules",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
)

# Where ``fusionscript.dll`` can live. The stock loader only falls back to
# the Free path; we explicitly try every edition.
_RESOLVE_LIB_CANDIDATES: Tuple[str, ...] = (
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\fusionscript.dll",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio 21 Beta\fusionscript.dll",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve 21 Beta\fusionscript.dll",
)

# Where ``Resolve.exe`` can live.
_RESOLVE_EXE_CANDIDATES: Tuple[str, ...] = (
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\Resolve.exe",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio 21 Beta\Resolve.exe",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve 21 Beta\Resolve.exe",
)

# Tuning.
RESOLVE_STARTUP_TIMEOUT_S = 90.0
RESOLVE_POLL_INTERVAL_S = 2.0
RESOLVE_DIAG_AFTER_S = 18.0

# Default preset fallback chain used by :func:`render_with_preset`. Override
# per-call by passing ``fallback_presets``.
DEFAULT_RENDER_PRESETS: Tuple[str, ...] = ("YouTube - 1080p", "H.264 Master")

_DAVINCI_MODULE: Any = None  # cached across calls


def register_custom_resolve_paths(
    *,
    modules_dir: Optional[str] = None,
    fusionscript_dll: Optional[str] = None,
    resolve_exe: Optional[str] = None,
    replace_defaults: bool = False,
) -> None:
    """Prepend or replace standard Resolve install-path candidates.

    Call **before** :func:`bootstrap_resolve_api` / :func:`connect_resolve`
    when Resolve lives outside the default folders. Paths may be missing on
    disk at registration time — they are still inserted so a later install
    can satisfy them.

    ``replace_defaults=True`` keeps only the entries you pass (plus any
    non-empty custom paths); use with care.

    Clears the cached DaVinci module handle so the next connection picks up
    the new ``RESOLVE_SCRIPT_*`` environment layout.
    """
    global _RESOLVE_MODULE_DIRS, _RESOLVE_LIB_CANDIDATES, _RESOLVE_EXE_CANDIDATES
    global _DAVINCI_MODULE

    def _norm(p: Optional[str]) -> Optional[str]:
        if not p:
            return None
        s = str(p).strip().strip('"')
        return s or None

    modules_dir = _norm(modules_dir)
    fusionscript_dll = _norm(fusionscript_dll)
    resolve_exe = _norm(resolve_exe)

    if replace_defaults:
        m_list: List[str] = []
        l_list: List[str] = []
        e_list: List[str] = []
    else:
        m_list = list(_RESOLVE_MODULE_DIRS)
        l_list = list(_RESOLVE_LIB_CANDIDATES)
        e_list = list(_RESOLVE_EXE_CANDIDATES)

    if modules_dir:
        if modules_dir in m_list:
            m_list.remove(modules_dir)
        m_list.insert(0, modules_dir)
    if fusionscript_dll:
        if fusionscript_dll in l_list:
            l_list.remove(fusionscript_dll)
        l_list.insert(0, fusionscript_dll)
    if resolve_exe:
        if resolve_exe in e_list:
            e_list.remove(resolve_exe)
        e_list.insert(0, resolve_exe)

    _RESOLVE_MODULE_DIRS = tuple(m_list)
    _RESOLVE_LIB_CANDIDATES = tuple(l_list)
    _RESOLVE_EXE_CANDIDATES = tuple(e_list)
    _DAVINCI_MODULE = None


# ---------------------------------------------------------------------------
# Public error type
# ---------------------------------------------------------------------------


class ResolveError(RuntimeError):
    """Raised when the Resolve API cannot be reached or returns something
    unexpected. Always carries a human-readable hint pointing at the likely
    root cause so errors surface usefully in logs / UI dialogs."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _first_existing(paths: Tuple[str, ...]) -> Optional[str]:
    for p in paths:
        if os.path.isfile(p) or os.path.isdir(p):
            return p
    return None


def to_forward(path: Any) -> str:
    """Convert any path to forward-slash form for the Resolve API.

    Use for every path handed to ``ImportMedia``, ``SetRenderSettings``,
    ``TargetDir`` etc. — backslashes silently break those calls on Windows.
    """
    return str(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# Process / environment discovery
# ---------------------------------------------------------------------------


def is_resolve_process_running() -> bool:
    """Return ``True`` if ``Resolve.exe`` currently appears in the task list.

    Use *only* to decide whether a ``Popen`` should be skipped — a second
    launch on an already-running Resolve wobbles the scripting socket and is
    the most common cause of mid-session breakage.

    Reads raw bytes from ``tasklist`` because non-English Windows uses OEM
    codepages that Python's implicit cp1252 decoder rejects.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        raw = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if not raw:
        return False
    return "Resolve.exe" in raw.decode("utf-8", errors="replace")


def running_resolve_exe() -> Optional[str]:
    """Return the full path to the currently-running ``Resolve.exe``, or None."""
    if not sys.platform.startswith("win"):
        return None
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-Process -Name Resolve -ErrorAction SilentlyContinue | "
                "Select-Object -First 1).Path",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.decode("utf-8", errors="replace").strip()
    if path and os.path.isfile(path):
        return path
    return None


def running_resolve_dir() -> Optional[str]:
    """Return the directory of the currently-running ``Resolve.exe``, or None.

    Lets callers prefer the exact edition's ``fusionscript.dll`` over the
    static candidate list — critical when multiple Resolve editions are
    installed side by side. Version mismatch between DLL and running
    Resolve breaks scripting silently.
    """
    exe = running_resolve_exe()
    return os.path.dirname(exe) if exe else None


def resolve_product_name(exe_path: Optional[str] = None) -> Optional[str]:
    """Return the PE ``ProductName`` string of ``Resolve.exe`` for logging.

    **Informational only — do NOT gate on this.** On Resolve 21 both the
    Free and Studio editions report the same ``ProductName`` ("DaVinci
    Resolve"), so using this to decide if the user is on Studio is a
    false-positive trap. Log it alongside the exe path for diagnostic
    clarity and let ``scriptapp()`` fail cleanly instead.
    """
    if exe_path is None:
        exe_path = running_resolve_exe()
    if not sys.platform.startswith("win") or not exe_path:
        return None
    try:
        ps_literal = exe_path.replace("'", "''")
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Item '{ps_literal}').VersionInfo.ProductName",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    product = out.decode("utf-8", errors="replace").strip()
    return product or None


def is_python_elevated() -> bool:
    """Return ``True`` if this Python process is running with admin rights.

    Privilege level isolates Windows' per-session scripting socket.
    Resolve + Python must run at the same level or ``scriptapp`` returns
    ``None`` forever regardless of preferences and paths.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - never crash on the check
        return False


def launch_resolve() -> bool:
    """Spawn ``Resolve.exe`` detached from the current process.

    Returns ``True`` if one of the candidate executables was found and
    launched, ``False`` otherwise. Caller should have checked
    :func:`is_resolve_process_running` first and skipped this call on a
    hit — launching a second instance destabilises the scripting socket.
    """
    for exe in _RESOLVE_EXE_CANDIDATES:
        if not os.path.isfile(exe):
            continue
        creation = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creation = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        try:
            subprocess.Popen(
                [exe],
                close_fds=True,
                creationflags=creation,
                cwd=os.path.dirname(exe),
            )
            return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Thread-safety helpers
# ---------------------------------------------------------------------------


@contextmanager
def scripting_thread() -> Iterator[None]:
    """Context manager that properly initialises COM for the current thread.

    DaVinci Resolve's ``fusionscript.dll`` uses COM internally. On Windows
    every thread that touches a COM object must first call
    ``CoInitializeEx`` — without it, ``scriptapp('Resolve')`` silently
    returns ``None`` from a worker thread even though it works fine from
    the main thread of a standalone script (classic "works in CLI, fails
    in GUI" trap).

    We pick MTA (``COINIT_MULTITHREADED`` / 0x0) because workers here
    don't run a Windows message pump. Matching ``CoUninitialize`` at exit
    keeps the thread clean. Safe to nest; only the outermost init pairs
    actually touch COM.

    Usage::

        def worker():
            with scripting_thread():
                resolve, project, *_ = connect_resolve()
                project.GetName()

    No-op on non-Windows platforms.
    """
    initialised = False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            # CoInitializeEx returns S_OK(0) or S_FALSE(1) when already
            # initialised — both are fine. Negative HRESULT means a real
            # failure; we silently skip in that case so a diagnostic helper
            # can't wedge the pipeline.
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)
            if hr >= 0:
                initialised = True
        except Exception:
            pass
    try:
        yield
    finally:
        if initialised:
            try:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bootstrap: import DaVinciResolveScript
# ---------------------------------------------------------------------------


def bootstrap_resolve_api() -> Any:
    """Import Blackmagic's ``DaVinciResolveScript`` module.

    Enforces DaVinci API rules 1 and 2:
      * purge stale env vars that survive across Resolve reinstalls / edition
        switches, then set them freshly from the first install location that
        actually exists on disk (preferring the edition of the currently
        running ``Resolve.exe``);
      * ``time.sleep(2)`` before importing so fusionscript.dll's file lock
        has been released after a fresh Resolve launch.

    The result is cached on module level — subsequent calls are no-ops.
    """
    global _DAVINCI_MODULE
    if _DAVINCI_MODULE is not None:
        return _DAVINCI_MODULE

    for key in ("RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB", "PYTHONPATH"):
        os.environ.pop(key, None)

    lib_path: Optional[str] = None
    modules_dir: Optional[str] = None

    # Prefer files from the edition whose Resolve.exe is actually running.
    running_dir = running_resolve_dir()
    if running_dir:
        dll_candidate = os.path.join(running_dir, "fusionscript.dll")
        if os.path.isfile(dll_candidate):
            lib_path = dll_candidate
        edition = os.path.basename(running_dir)
        mirrored_modules = os.path.join(
            r"C:\ProgramData\Blackmagic Design",
            edition,
            r"Support\Developer\Scripting\Modules",
        )
        if os.path.isdir(mirrored_modules):
            modules_dir = mirrored_modules

    if modules_dir is None:
        modules_dir = _first_existing(_RESOLVE_MODULE_DIRS)
    if lib_path is None:
        lib_path = _first_existing(_RESOLVE_LIB_CANDIDATES)
    if modules_dir is None or lib_path is None:
        raise ResolveError(
            "Could not locate the DaVinci Resolve scripting files. Tried "
            "Modules dirs: "
            + ", ".join(_RESOLVE_MODULE_DIRS)
            + " | fusionscript.dll: "
            + ", ".join(_RESOLVE_LIB_CANDIDATES)
        )

    os.environ["RESOLVE_SCRIPT_API"] = os.path.dirname(modules_dir)
    os.environ["RESOLVE_SCRIPT_LIB"] = lib_path

    time.sleep(2)

    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)

    # NOTE: Resolve 21's DaVinciResolveScript.py ends with
    # ``sys.modules[__name__] = script_module``. If the DLL fails to load
    # the whole import raises ImportError — there is no silent-``None``
    # state to guard against after this call.
    import DaVinciResolveScript as dvr_script  # type: ignore  # noqa: E402
    _DAVINCI_MODULE = dvr_script
    return dvr_script


def _poll_for_scriptapp(
    dvr_script: Any,
    log: Callable[[str], None],
) -> Any:
    """Poll ``scriptapp("Resolve")`` until it returns a live object or times
    out. Returns the object or ``None``."""
    start = time.monotonic()
    deadline = start + RESOLVE_STARTUP_TIMEOUT_S
    last_heartbeat = start
    attempt = 0
    diag_printed = False
    preference_hint_logged = False
    while time.monotonic() < deadline:
        attempt += 1
        time.sleep(RESOLVE_POLL_INTERVAL_S)
        resolve = dvr_script.scriptapp("Resolve")
        if resolve is not None:
            log(
                f"Resolve is up after ~{time.monotonic() - start:.0f}s "
                f"(attempt {attempt})."
            )
            return resolve

        # Early actionable hint: Resolve's process is clearly up but the
        # scripting socket never answers. Telling the user after ~4s is far
        # more useful than waiting 18s for the generic diag dump.
        if (
            not preference_hint_logged
            and attempt >= 2
            and is_resolve_process_running()
        ):
            log(
                "Resolve is running but not answering scripting. "
                "Fix: Preferences → System → General → 'External "
                "scripting using' = Local, THEN restart Resolve."
            )
            preference_hint_logged = True

        now = time.monotonic()
        if not diag_printed and now - start >= RESOLVE_DIAG_AFTER_S:
            log(
                "Still no scripting response after "
                f"{RESOLVE_DIAG_AFTER_S:.0f}s — check: "
                "(1) External scripting = Local AND Resolve restarted since "
                "you toggled it, (2) your worker thread wrapped the call in "
                "scripting_thread() so COM is initialised, (3) Resolve and "
                "Python run at the same privilege level (both admin or both "
                "user), (4) a project is loaded (not Project Manager)."
            )
            diag_printed = True
        if now - last_heartbeat >= 8.0:
            log(
                "Waiting for Resolve scripting server… "
                f"({max(0.0, deadline - now):.0f}s left)"
            )
            last_heartbeat = now
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def connect_resolve(
    *,
    status_callback: Optional[Callable[[str], None]] = None,
    auto_launch: bool = True,
    create_scratch_project_name: Optional[str] = "AutoPipeline",
) -> Tuple[Any, Any, Any, Any]:
    """Connect to a Resolve instance and return ``(resolve, project, media_pool, root_folder)``.

    Parameters
    ----------
    status_callback:
        Receives human-readable progress strings ("Launching…", "Waiting for
        scripting server… 72s left"). Wire this to your UI status bar.
    auto_launch:
        When ``True`` and Resolve is not running, ``Resolve.exe`` is started
        automatically. When ``False`` a missing Resolve raises immediately.
    create_scratch_project_name:
        When non-empty and no project is open after connecting, a new project
        with this base name (plus a timestamp) is created. Set to ``None`` to
        fail hard instead — useful for tools that must never touch the
        Project Manager.

    Must be called from a thread where COM is initialised (use the
    :func:`scripting_thread` context manager on GUI worker threads).

    Raises
    ------
    ResolveError
        When scripting files can't be located, when Resolve can't be launched
        / reached within the timeout, or when no project is open and no
        scratch name was supplied.
    """
    def _log(msg: str) -> None:
        if status_callback is not None:
            status_callback(msg)

    dvr_script = bootstrap_resolve_api()

    # Surface exactly what bound — invaluable when tracking down silent
    # connection failures. Same info the pipeline's error message shows
    # below on timeout, logged upfront so the user sees it at startup too.
    import platform

    lib_env = os.environ.get("RESOLVE_SCRIPT_LIB", "?")
    api_env = os.environ.get("RESOLVE_SCRIPT_API", "?")
    running_exe = running_resolve_exe()
    product = resolve_product_name(running_exe) if running_exe else None
    py_admin = "admin" if is_python_elevated() else "user"

    _log(
        f"Python: {sys.version.split()[0]} "
        f"({platform.architecture()[0]}) [{py_admin}]"
    )
    _log(f"Scripting lib: {lib_env}")
    _log(f"Scripting API: {api_env}")
    if running_exe:
        _log(
            f"Running exe:   {running_exe}"
            + (f"  ({product})" if product else "")
        )

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        if auto_launch and not is_resolve_process_running():
            _log("DaVinci Resolve not running — launching…")
            if not launch_resolve():
                raise ResolveError(
                    "Could not find Resolve.exe under any of the default "
                    "install paths: " + ", ".join(_RESOLVE_EXE_CANDIDATES)
                )
        elif is_resolve_process_running():
            _log("Resolve is starting — waiting for scripting server…")
        elif not auto_launch:
            raise ResolveError(
                "DaVinci Resolve is not running. Start Resolve Studio and "
                "open a project first."
            )

        resolve = _poll_for_scriptapp(dvr_script, _log)
        if resolve is None:
            running_exe_now = running_resolve_exe() or "(Resolve.exe not detected)"
            raise ResolveError(
                f"Could not connect to Resolve within "
                f"{RESOLVE_STARTUP_TIMEOUT_S:.0f}s.\n\n"
                f"Python:        {sys.version.split()[0]} "
                f"({platform.architecture()[0]}) [{py_admin}]\n"
                f"Running exe:   {running_exe_now}\n"
                f"Scripting lib: {lib_env}\n"
                f"Scripting API: {api_env}\n\n"
                "Remaining causes when paths look right: "
                "(1) 'External scripting using' isn't 'Local' in "
                "Preferences → System → General (toggle requires a Resolve "
                "restart, Save alone is not enough); "
                "(2) the calling thread did not initialise COM — wrap the "
                "call in `with scripting_thread(): ...`; "
                "(3) Resolve was started as admin while Python runs as "
                f"'{py_admin}' — run both at the same privilege level; "
                "(4) a modal dialog is blocking Resolve's UI thread "
                "(click through any 'Welcome' / 'Quick Setup' wizard); "
                "(5) Resolve is on the Project Manager screen — double-"
                "click a project so scriptapp() can bind."
            )

    project_manager = resolve.GetProjectManager()
    if project_manager is None:
        raise ResolveError("Could not access the Project Manager.")

    project = project_manager.GetCurrentProject()
    if project is None:
        if not create_scratch_project_name:
            raise ResolveError(
                "No project is open in Resolve. Open or create a project "
                "before running this tool."
            )
        fallback_name = f"{create_scratch_project_name}_{int(time.time())}"
        _log(f"No project open — creating scratch project {fallback_name!r}…")
        project = project_manager.CreateProject(fallback_name)
        if project is None:
            raise ResolveError(
                "No project is open and a fallback project could not be "
                "created. Open a project in Resolve and retry."
            )

    media_pool = project.GetMediaPool()
    if media_pool is None:
        raise ResolveError("Could not access the Media Pool.")
    root_folder = media_pool.GetRootFolder()

    return resolve, project, media_pool, root_folder


# ---------------------------------------------------------------------------
# Project-side helpers (timeline settings + render presets)
# ---------------------------------------------------------------------------


def cleanup_timelines(
    project: Any,
    media_pool: Any,
    *,
    name_prefix: Optional[str] = None,
) -> int:
    """Delete existing timelines so the project frame rate unlocks.

    Resolve silently refuses ``SetSetting('timelineFrameRate', …)`` as
    long as *any* timeline exists in the project at a different rate.
    That's the #1 reason "setting the FPS doesn't stick" — a leftover
    timeline from a prior run locks it.

    Pass ``name_prefix`` to delete only timelines whose name starts with
    that string (e.g. ``"AutoRun_"``) so user-created timelines stay
    untouched. Pass ``None`` to wipe them all — only safe in a
    freshly-created scratch project.

    Returns the number of timelines actually removed.
    """
    try:
        count = int(project.GetTimelineCount() or 0)
    except Exception:
        return 0
    if count <= 0:
        return 0

    victims: List[Any] = []
    for i in range(1, count + 1):
        try:
            tl = project.GetTimelineByIndex(i)
        except Exception:
            continue
        if not tl:
            continue
        if name_prefix is not None:
            try:
                if not str(tl.GetName() or "").startswith(name_prefix):
                    continue
            except Exception:
                continue
        victims.append(tl)
    if not victims:
        return 0
    try:
        media_pool.DeleteTimelines(victims)
    except Exception:
        return 0
    return len(victims)


def _normalise_fps(fps: Any) -> str:
    """Coerce a clip's reported FPS into the exact string Resolve accepts.

    ``SetSetting('timelineFrameRate', …)`` is pedantic: "25" is accepted,
    "25.0" is often rejected silently; "29.97" works, "29.97002997" may
    not. We keep the original string form when the clip API hands us one
    and only collapse "X.0" → "X" so it matches Resolve's own preset
    values. Returns ``"25"`` on anything unparseable.
    """
    fps_str = str(fps).strip() if fps is not None else ""
    try:
        fps_float = float(fps_str.split()[0])
        if fps_float <= 0:
            raise ValueError
    except (TypeError, ValueError, IndexError):
        return "25"
    if fps_float.is_integer():
        return str(int(fps_float))
    # Keep the user's original fractional form verbatim ("29.97",
    # "23.976") — Resolve reports them exactly the same way via
    # GetClipProperty, and bit-identical strings are the only reliable
    # input.
    return fps_str.split()[0]


def apply_project_timeline_settings(
    project: Any, fps: Any, resolution: Any
) -> Tuple[int, int, str]:
    """Force the project's timeline settings to match a source clip so any
    timeline created next inherits matching width/height/FPS.

    Must be called BEFORE ``media_pool.CreateEmptyTimeline(...)`` —
    ``CreateEmptyTimeline`` snapshots the project settings at creation
    time. Setting them afterwards has no effect on the active timeline.
    Also call :func:`cleanup_timelines` first if the project already
    has timelines at a different rate: Resolve refuses rate changes
    while timelines exist, and fails silently.

    Parses ``resolution`` as ``"1920x1080"`` / ``"1920 x 1080"`` (both
    are seen depending on codec). Falls back to 1920×1080 @ "25" on
    unparseable metadata (Rule 6).

    Returns
    -------
    (width, height, fps_str_actually_applied) — the FPS comes from a
    ``GetSetting`` read-back, so if Resolve silently rejected the
    change you see the stale value and can act on it.
    """
    # Parse resolution with whitespace tolerance.
    width, height = 1920, 1080
    try:
        w_str, h_str = str(resolution).lower().replace(" ", "").split("x", 1)
        width = int(w_str)
        height = int(h_str)
    except (ValueError, AttributeError):
        pass

    fps_str = _normalise_fps(fps)

    project.SetSetting("timelineResolutionWidth", str(width))
    project.SetSetting("timelineResolutionHeight", str(height))
    project.SetSetting("timelineFrameRate", fps_str)

    # Tiny settle — Resolve sometimes lags one poll behind when you
    # SetSetting + GetSetting + CreateEmptyTimeline back to back.
    time.sleep(0.3)

    try:
        applied = project.GetSetting("timelineFrameRate") or fps_str
        applied = str(applied).strip() or fps_str
    except Exception:
        applied = fps_str
    return width, height, applied


def list_render_presets(project: Any) -> List[str]:
    """Return every render-preset name available in the project (factory +
    user), de-duplicated and case-insensitively sorted.

    Useful for populating a render-preset dropdown in a UI. Returns an
    empty list on any API failure — callers should fall back to the
    default preset chain in :func:`render_with_preset`.
    """
    try:
        names = project.GetRenderPresetList() or []
    except Exception:
        return []

    seen: set = set()
    unique: List[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)
    unique.sort(key=str.lower)
    return unique


# Small settle after ImportMedia before CreateEmptyTimeline / AppendToTimeline
# (clip metadata/proxy hooks; matches production pipelines using this kit).
APPEND_SUBCLIP_SLEEP_AFTER_IMPORT_SEC = 0.35


def append_dict_for_subclip(
    media_pool_item: Any,
    start_frame_inclusive: int,
    end_frame_inclusive: int,
) -> Optional[Dict[str, Any]]:
    """Build one ``AppendToTimeline`` dict for a **trimmed** source range.

    Resolve uses a **half-open** interval on the source clip::

        [startFrame, endFrame)   # endFrame is EXCLUSIVE

    So if your pipeline computes **inclusive** last frame ``last_i``, pass
    ``end_frame_inclusive=last_i`` here — this helper sends
    ``endFrame = last_i + 1`` to the API.

    Production reference (same convention): ``compare.py`` /
    ``_davinci_run_export_pipeline`` in *Deepfake_smoother_premium* — only
    ``mediaPoolItem``, ``startFrame``, and ``endFrame`` are required for
    reliable subclips.

    **Compilation (multiple files, one timeline):** build one list of these dicts
    (different ``mediaPoolItem`` per segment as needed) and call
    ``AppendToTimeline(list)`` **once**, typically **without** ``recordFrame``.
    Do not advance ``recordFrame`` in **source** frames — see ``README.md``
    “Compilation mode” and ``COMPILATION_MODE_PROMPT.md`` in this kit.

    Returns ``None`` if the range would yield zero duration (invalid).
    """
    start = int(start_frame_inclusive)
    end_excl = int(end_frame_inclusive) + 1
    if end_excl <= start:
        return None
    return {
        "mediaPoolItem": media_pool_item,
        "startFrame": start,
        "endFrame": end_excl,
    }


def render_with_preset(
    project: Any,
    *,
    output_dir: str,
    output_name: str,
    preset_name: Optional[str] = None,
    fallback_presets: Tuple[str, ...] = DEFAULT_RENDER_PRESETS,
    timeout_s: float = 3600.0,
    status_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Configure and execute a single render job, monitored with a timeout.

    Rule 7: purges the queue before queueing (``DeleteAllRenderJobs``).
    Rule 8: preset fallback chain + bounded polling loop.

    Tries ``preset_name`` first (if given); on failure walks
    ``fallback_presets`` in order until one loads. Raises if none of them
    do — surfaces the list of attempted names so the user can fix their
    preset selection.

    Parameters
    ----------
    output_dir:
        Directory for the rendered file. Converted to forward slashes
        automatically (Rule 3).
    output_name:
        Base filename (without extension) — the preset determines the
        extension via ``FormatStr``.
    preset_name:
        Preferred render preset. ``None`` means "skip straight to the
        fallback chain".
    fallback_presets:
        Presets to try if ``preset_name`` doesn't load. Defaults to
        ``("YouTube - 1080p", "H.264 Master")``.
    timeout_s:
        Hard stop for the render polling loop. Aborts the render on
        expiry so a frozen Resolve doesn't hang the caller forever.
    status_callback:
        Receives progress strings. Same shape as ``connect_resolve``'s.
    """
    def _log(msg: str) -> None:
        if status_callback is not None:
            status_callback(msg)

    project.DeleteAllRenderJobs()

    tried: List[str] = []
    loaded = False
    for candidate in (preset_name, *fallback_presets):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if project.LoadRenderPreset(candidate):
            _log(f"Render preset loaded: {candidate}")
            loaded = True
            break
    if not loaded:
        raise ResolveError(
            "Could not load any render preset. Tried: "
            + ", ".join(tried)
            + ". Check the names in Resolve's Deliver page."
        )

    project.SetRenderSettings(
        {
            "TargetDir": to_forward(output_dir),
            "CustomName": output_name,
        }
    )

    job_id = project.AddRenderJob()
    if not job_id:
        raise ResolveError("Resolve refused to queue the render job.")
    project.StartRendering(job_id)

    started = time.time()
    while project.IsRenderingInProgress():
        if time.time() - started > timeout_s:
            project.StopRendering()
            raise ResolveError(
                f"Render exceeded the {timeout_s:.0f}s timeout and was aborted."
            )
        time.sleep(1.0)
    _log("Render finished.")


# ---------------------------------------------------------------------------
# Standalone demo
# ---------------------------------------------------------------------------


def _demo() -> int:
    """Connect, print project info, and list render presets.

    Returns a shell-style exit code (0 on success, 1 on failure) so you
    can use it as a quick smoke test::

        python davinci_api.py && echo "all good"
    """
    with scripting_thread():
        try:
            resolve, project, media_pool, root_folder = connect_resolve(
                status_callback=lambda m: print(f"[status] {m}"),
                auto_launch=True,
            )
        except ResolveError as err:
            print(f"[error] {err}", file=sys.stderr)
            return 1

        clip_count = len(root_folder.GetClipList() or [])
        print(f"Connected — product: {resolve.GetProductName()}")
        print(f"           project: {project.GetName()}")
        print(f"     media-pool clips (root folder): {clip_count}")

        presets = list_render_presets(project)
        if presets:
            print(f"     render presets available ({len(presets)}):")
            for name in presets[:15]:
                print(f"       - {name}")
            if len(presets) > 15:
                print(f"       … and {len(presets) - 15} more")
        else:
            print("     render presets: (none reported by the API)")
        return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
