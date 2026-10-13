"""Desktop-first native Tk workspace; the separate worker retains trading ownership."""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from oandafly import __version__
from oandafly.ui.model import command_confirmation, status_view, validate_authority


class BackgroundTasks:
    """One daemon performs bounded local tasks. Only the main thread touches Tk."""
    def __init__(self) -> None:
        self.requests: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="oandafly-desktop-io")
        self.thread.start()

    def _run(self) -> None:
        while True:
            task = self.requests.get()
            if task is None:
                return
            key, function, callback = task
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, type(exc).__name__
            if not self.closed:
                self.results.put((key, callback, result, error))

    def submit(self, key: str, function: Callable, callback: Callable) -> None:
        if not self.closed:
            self.requests.put((key, function, callback))

    def close(self) -> None:
        self.closed = True
        self.requests.put(None)


class DesktopApplication:
    def __init__(self, root: Any, *, backend: Any = None, authority: str | None = None,
                 configuration: Path | None = None, verification: bool = False) -> None:
        import tkinter as tk
        from tkinter import ttk
        if backend is None:
            from oandafly.ui.controller import DesktopBackend, VerificationBackend
            backend = VerificationBackend() if verification else DesktopBackend()
        self.root, self.backend, self.verification = root, backend, verification
        self.tasks = BackgroundTasks()
        self.closed = False
        self.configuration_path: Path | None = None
        self.configuration_summary: dict | None = None
        self.selection_generation = 0
        self.attached_authority: str | None = None
        self.status_pending = False
        self.poll_after: Any = None
        self.drain_after: Any = None
        root.title(f"Oandafly {__version__}")
        root.geometry("1080x760")
        root.minsize(850, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._callback_error
        style = ttk.Style(root)
        # Keep the native theme and platform fonts; respect OS accessibility scaling.
        style.configure("Title.TLabel", font=("TkDefaultFont", 21, "bold"))
        style.configure("Section.TLabel", font=("TkDefaultFont", 12, "bold"))
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Oandafly", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Full MaleCNS policy  ·  Independent risk  ·  Authorized execution").pack(anchor="w", pady=(2, 12))
        self.message = tk.StringVar(value="Welcome. No account is connected and no trading has been started.")
        ttk.Label(frame, textvariable=self.message, wraplength=990).pack(anchor="w", pady=(0, 12))
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        workspace, diagnostics, setup = (ttk.Frame(notebook, padding=16) for _ in range(3))
        notebook.add(workspace, text="Workspace")
        notebook.add(diagnostics, text="Diagnostics")
        notebook.add(setup, text="Setup & safety")
        connect = ttk.LabelFrame(workspace, text="Local account worker", padding=12)
        connect.pack(fill="x")
        self.authority = tk.StringVar(value=authority or "")
        ttk.Label(connect, text="Authority").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(connect, textvariable=self.authority).grid(row=0, column=1, sticky="ew")
        self.attach_button = ttk.Button(connect, text="Attach status", command=self.attach)
        self.attach_button.grid(row=0, column=2, padx=8)
        self.choose_button = ttk.Button(connect, text="Open worker configuration…", command=self.choose)
        self.choose_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.start_button = ttk.Button(connect, text="Start authorized worker", command=self.start, state="disabled")
        self.start_button.grid(row=1, column=2, pady=(12, 0))
        connect.columnconfigure(1, weight=1)
        self.summary = tk.StringVar(value="No configuration selected. Desktop launch never arms an account.")
        ttk.Label(connect, textvariable=self.summary, wraplength=930).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.health = tk.StringVar(value="NOT CONNECTED — no readiness inferred")
        ttk.Label(workspace, textvariable=self.health, style="Section.TLabel", wraplength=940).pack(anchor="w", pady=(16, 8))
        body = ttk.Frame(workspace)
        body.pack(fill="both", expand=True)
        self.status_text = tk.Text(body, wrap="word", height=12, state="disabled", takefocus=True)
        self.status_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, command=self.status_text.yview)
        scroll.pack(side="right", fill="y")
        self.status_text.configure(yscrollcommand=scroll.set)
        controls = ttk.Frame(workspace)
        controls.pack(fill="x", pady=(12, 0))
        self.control_buttons = []
        for operation in ("HALT", "FLATTEN", "RESUME", "STOP"):
            button = ttk.Button(controls, text=operation, state="disabled", command=lambda op=operation: self.command(op))
            button.pack(side="left", padx=(0, 8))
            self.control_buttons.append(button)
        ttk.Label(workspace, text="Commands are queued, not assumed executed. STOP does not flatten. Closing this window leaves the worker running.",
                  wraplength=950).pack(anchor="w", pady=(8, 0))
        ttk.Label(diagnostics, text="Desktop capability is separate from trading qualification.", style="Section.TLabel").pack(anchor="w")
        ttk.Label(diagnostics, text="Native local production inference currently requires Linux + libseccomp. macOS and Windows desktop tools do not bypass that boundary.",
                  wraplength=920).pack(anchor="w", pady=8)
        self.diagnostic_text = tk.Text(diagnostics, state="disabled", wrap="word")
        self.diagnostic_text.pack(fill="both", expand=True)
        ttk.Button(diagnostics, text="Refresh diagnostics", command=self.diagnostics).pack(anchor="w", pady=(10, 0))
        instructions = (
            "1. Inspect Diagnostics. A working desktop is not a qualified trading account.\n\n"
            "2. Prepare a private worker configuration and independently reviewed graph/model artifacts. "
            "Practice, Shadow and Live use separate scopes. Never paste API tokens into configuration files.\n\n"
            "3. Open the configuration to check its schema and scope. This does not start a worker or verify approvals.\n\n"
            "4. Start explicitly, then inspect the worker’s status and acknowledgements. The worker independently checks all grants, limits and evidence.\n\n"
            "SHADOW has no broker mutation capability. LIVE requires separately scoped authorization. "
            "No strategy fallback, fabricated readiness, automatic account enrollment or live-mode shortcut is provided.\n\n"
            "Native builds: macOS ARM64, Windows AMD64 and Linux AMD64. "
            "For full setup see docs/DESKTOP.md and docs/BOOTSTRAP.md in the source project."
        )
        ttk.Label(setup, text=instructions, wraplength=920, justify="left").pack(anchor="nw")
        try:
            from importlib.resources import files
            icon = files("oandafly").joinpath("resources/icons/oandafly-128.png")
            self.icon = tk.PhotoImage(data=__import__("base64").b64encode(icon.read_bytes()))
            root.iconphoto(True, self.icon)
        except (OSError, tk.TclError):
            pass  # Native bundle icons remain available; a missing desktop icon is not authority.
        if verification:
            self.attach_button.configure(state="disabled")
            self.choose_button.configure(state="disabled")
            self.message.set("BUILD VERIFICATION — no config, vault, broker, database or worker access")
        self.diagnostics()
        self.drain_after = root.after(50, self._drain)
        if not verification and configuration is not None:
            self.load_configuration(configuration)
        elif not verification and authority:
            self.attach()

    @staticmethod
    def _write(widget: Any, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _callback_error(self, error: type, value: Any, traceback: Any) -> None:
        self.message.set("Desktop operation failed: " + error.__name__ + ". No completion is assumed.")
        if self.verification:
            self.verification_error = error.__name__

    def _drain(self) -> None:
        if self.closed:
            return
        try:
            while True:
                _, callback, result, error = self.tasks.results.get_nowait()
                try:
                    callback(result, error)
                except Exception as exc:
                    # One failed presentation callback must not stop polling or
                    # prevent later controls from receiving their results.
                    self._callback_error(type(exc), exc, exc.__traceback__)
        except queue.Empty:
            pass
        self.drain_after = self.root.after(50, self._drain)

    def diagnostics(self) -> None:
        def completed(result: Any, error: str | None) -> None:
            self._write(self.diagnostic_text, "Diagnostics unavailable: " + error if error else json.dumps(result, indent=2))
        self.tasks.submit("diagnostics", self.backend.diagnostics, completed)

    def choose(self) -> None:
        if self.verification:
            return
        from tkinter import filedialog
        value = filedialog.askopenfilename(parent=self.root, title="Open private worker configuration",
                                          filetypes=[("Worker JSON", "*.json"), ("All files", "*")])
        if value:
            self.load_configuration(Path(value))

    def load_configuration(self, path: Path) -> None:
        if self.verification:
            return
        self.selection_generation += 1
        generation = self.selection_generation
        self.configuration_path = None
        self.configuration_summary = None
        self.start_button.configure(state="disabled")
        self.message.set("Validating private configuration metadata; no account is being started.")
        def completed(result: dict | None, error: str | None) -> None:
            if generation != self.selection_generation:
                return
            if error:
                self.summary.set("Configuration not accepted: " + error + ". Check its schema and private file permissions.")
                self.message.set("Configuration validation failed. No worker was started.")
                return
            self.configuration_path, self.configuration_summary = path.absolute(), result
            self.authority.set(result["authority_id"])
            self.summary.set(f"{result['mode']} / {result['environment']} / {result['account_id']} — schema valid; approvals NOT verified")
            self.message.set("Configuration loaded. No worker has been started.")
            if result["local_worker_platform_available"]:
                self.start_button.configure(state="normal")
            else:
                self.message.set("Local autonomous inference is not qualified on this OS. Desktop/configuration tools remain available.")
            self.attach()
        self.tasks.submit("configuration", lambda: self.backend.configuration(path), completed)

    def attach(self) -> None:
        if self.verification:
            return
        try:
            authority = validate_authority(self.authority.get().strip())
        except ValueError as exc:
            self.message.set(str(exc))
            return
        self.attached_authority = authority
        for button in self.control_buttons:
            button.configure(state="disabled")
        self.health.set("Waiting for observed status — no readiness inferred")
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after = None
        self.refresh()

    def refresh(self) -> None:
        if self.closed or not self.attached_authority or self.verification:
            return
        authority = self.attached_authority
        if self.status_pending:
            self.poll_after = self.root.after(500, self.refresh)
            return
        self.status_pending = True
        def completed(result: dict | None, error: str | None) -> None:
            self.status_pending = False
            if authority != self.attached_authority:
                return
            if error:
                self.health.set("STATUS UNAVAILABLE — no readiness inferred")
                self._write(self.status_text, "No readable worker status: " + error)
                for button in self.control_buttons:
                    button.configure(state="disabled")
            else:
                try:
                    view = status_view(result, authority)
                    self.health.set(f"{view['mode']} / {view['environment']} · {view['freshness']} · Safety: {view['safety']}")
                    self._write(self.status_text, json.dumps(result, indent=2))
                    for button in self.control_buttons:
                        button.configure(state="normal")
                except (ValueError, TypeError):
                    self.health.set("INVALID STATUS — no readiness inferred")
                    for button in self.control_buttons:
                        button.configure(state="disabled")
        self.tasks.submit("status", lambda: self.backend.status(authority), completed)
        self.poll_after = self.root.after(2000, self.refresh)

    def command(self, operation: str) -> None:
        if self.verification or self.attached_authority is None:
            return
        from tkinter import messagebox
        authority = self.attached_authority
        if not messagebox.askyesno("Confirm " + operation, authority + "\n\n" + command_confirmation(operation), parent=self.root):
            return
        def completed(result: str | None, error: str | None) -> None:
            self.message.set(f"{authority}: " + ("command not queued: " + error if error else f"{operation} queued: {result}. Await worker acknowledgement."))
        self.tasks.submit("control", lambda: self.backend.control(authority, operation), completed)

    def start(self) -> None:
        if self.verification or self.configuration_path is None or self.configuration_summary is None:
            return
        from tkinter import messagebox
        path, summary = self.configuration_path, dict(self.configuration_summary)
        if not messagebox.askyesno("Start account worker", f"Start {summary['mode']} / {summary['environment']} for {summary['account_id']}?\n\n"
                                  "This may place orders when existing independent authorization and all gates permit. "
                                  "Closing this desktop will not stop the worker.", parent=self.root):
            return
        self.start_button.configure(state="disabled")
        def completed(result: dict | None, error: str | None) -> None:
            self.message.set("Worker not started: " + error if error else json.dumps(result))
            if self.configuration_path == path:
                self.start_button.configure(state="normal")
        self.tasks.submit("start", lambda: self.backend.start(path, summary), completed)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.tasks.close()
        for token in (self.drain_after, self.poll_after):
            if token is not None:
                self.root.after_cancel(token)
        self.root.destroy()


def launch(authority_id: str | None = None, configuration: Path | None = None) -> None:
    import tkinter as tk
    from oandafly.platforms import native_target
    native_target()
    root = tk.Tk()
    DesktopApplication(root, authority=authority_id, configuration=configuration)
    root.mainloop()
