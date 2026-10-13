"""Real main-thread Tk tests; missing displays fail when native builds require them."""
from __future__ import annotations

import os
import threading
import time

import pytest

from oandafly.ui.app import DesktopApplication

pytestmark = pytest.mark.desktop_gui


@pytest.fixture
def root():
    import tkinter as tk
    try:
        window=tk.Tk()
    except tk.TclError:
        if os.environ.get('OANDAFLY_GUI_TEST_REQUIRED') == '1':
            pytest.fail('A native display is required for desktop build verification')
        pytest.skip('No Tk display; native build gate must run these tests with a display')
    yield window
    try:
        if window.winfo_exists(): window.destroy()
    except tk.TclError:
        pass


def pump(root, predicate, seconds=3):
    until=time.monotonic()+seconds
    while time.monotonic()<until and not predicate():
        root.update()
        time.sleep(.01)
    assert predicate(), 'UI task did not complete within its local test bound'


class FakeBackend:
    def __init__(self): self.calls=[]
    def diagnostics(self):
        self.calls.append(('diagnostics',threading.current_thread().name))
        return {'test_fixture':True,'trading_qualified':False}
    def configuration(self,path):
        self.calls.append(('configuration',str(path)))
        return {'authority_id':'test','environment':'SANDBOX','mode':'SHADOW','account_id':'test-account',
                'instruments':['EUR_USD'],'local_worker_platform_available':True}
    def status(self,authority):
        self.calls.append(('status',authority))
        raise FileNotFoundError()
    def start(self,*args):
        self.calls.append(('start',args))
        return {'state':'FIXTURE_STARTED','trading_ready':False}
    def control(self,*args):
        self.calls.append(('control',args))
        return 'fixture-request'


def test_default_desktop_visible_without_account_or_start(root):
    backend=FakeBackend(); app=DesktopApplication(root,backend=backend)
    pump(root,lambda:any(x[0]=='diagnostics' for x in backend.calls))
    root.update()
    assert root.winfo_ismapped()
    assert app.configuration_path is None
    assert app.attached_authority is None
    assert not any(x[0] in {'configuration','start','status','control'} for x in backend.calls)
    assert str(app.start_button['state'])=='disabled'
    app.close()


def test_slow_local_io_does_not_block_tk_main_thread(root):
    entered=threading.Event(); release=threading.Event()
    class Slow(FakeBackend):
        def diagnostics(self):
            entered.set(); release.wait(3)
            return {'complete':True}
    app=DesktopApplication(root,backend=Slow())
    try:
        pump(root,entered.is_set)
        beats=[]; root.after(20,lambda:beats.append(True))
        pump(root,lambda:bool(beats),seconds=1)
        assert not release.is_set()
    finally:
        release.set(); app.close()


def test_configuration_selection_never_starts_worker(root,tmp_path):
    backend=FakeBackend(); app=DesktopApplication(root,backend=backend)
    app.load_configuration(tmp_path/'configuration.json')
    pump(root,lambda:app.configuration_summary is not None)
    assert app.attached_authority=='test'
    assert not any(x[0]=='start' for x in backend.calls)
    assert 'NOT verified' in app.summary.get()
    app.close()


def test_platform_limitation_disables_worker_start(root,tmp_path):
    class Unsupported(FakeBackend):
        def configuration(self,path):
            result=super().configuration(path); result['local_worker_platform_available']=False
            return result
    app=DesktopApplication(root,backend=Unsupported())
    app.load_configuration(tmp_path/'config.json')
    pump(root,lambda:app.configuration_summary is not None)
    assert str(app.start_button['state'])=='disabled'
    assert 'not qualified on this OS' in app.message.get()
    app.close()


def test_verification_workspace_cannot_attach_or_start(root):
    app=DesktopApplication(root,verification=True)
    app.authority.set('test'); app.attach(); app.start(); app.command('FLATTEN')
    assert app.attached_authority is None
    assert str(app.attach_button['state']) == str(app.choose_button['state']) == 'disabled'
    assert 'no config, vault, broker' in app.message.get()
    app.close()


def test_window_close_does_not_send_stop_or_kill(root):
    backend=FakeBackend(); app=DesktopApplication(root,backend=backend)
    app.close()
    assert app.closed
    assert not any(x[0] in {'control','start'} for x in backend.calls)


def test_cancelled_confirmation_sends_no_control(root,monkeypatch):
    backend=FakeBackend(); app=DesktopApplication(root,backend=backend)
    app.attached_authority='test'
    monkeypatch.setattr('tkinter.messagebox.askyesno',lambda *args,**kwargs:False)
    app.command('FLATTEN')
    assert not any(x[0]=='control' for x in backend.calls)
    app.close()


def test_confirmed_command_shows_queued_not_execution(root,monkeypatch):
    backend=FakeBackend(); app=DesktopApplication(root,backend=backend)
    app.attached_authority='test'
    monkeypatch.setattr('tkinter.messagebox.askyesno',lambda *args,**kwargs:True)
    app.command('HALT')
    pump(root,lambda:'queued' in app.message.get())
    assert 'Await worker acknowledgement' in app.message.get()
    assert ('control',('test','HALT')) in backend.calls
    app.close()


def test_callbacks_use_main_thread_and_errors_are_redacted(root):
    app=DesktopApplication(root,backend=FakeBackend())
    app._callback_error(ValueError,ValueError('SECRET_TOKEN'),None)
    assert 'ValueError' in app.message.get()
    assert 'SECRET_TOKEN' not in app.message.get()
    app.close()


def test_failed_presentation_callback_does_not_stop_result_pump(root):
    app = DesktopApplication(root, backend=FakeBackend())
    completed = []
    def fail(result, error):
        raise ValueError("PRIVATE_CALLBACK_DETAIL")
    app.tasks.results.put(("broken", fail, None, None))
    app.tasks.results.put(("following", lambda result, error: completed.append(True), None, None))
    pump(root, lambda: bool(completed))
    assert "ValueError" in app.message.get()
    assert "PRIVATE_CALLBACK_DETAIL" not in app.message.get()
    assert app.drain_after is not None
    app.close()
