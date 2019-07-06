"""
Dwarf - Copyright (C) 2019 Giovanni Rocca (iGio90)

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>
"""
import os
import binascii
import frida
import importlib.util
import json

from PyQt5.QtCore import QObject, pyqtSignal, QThread
from PyQt5.QtWidgets import QFileDialog, QApplication

from frida.core import Session

from lib import utils
from lib.context import Context
from lib.hook import Hook, HOOK_ONLOAD, HOOK_NATIVE, HOOK_JAVA, HOOK_WATCHER
from lib.kernel import Kernel

from ui.dialog_input import InputDialog


class Dwarf(QObject):
    class NoDeviceAssignedError(Exception):
        """ Raised when no Device
        """

    class CoreScriptNotFoundError(Exception):
        """ Raised when dwarfscript not found
        """

    # ************************************************************************
    # **************************** Signals ***********************************
    # ************************************************************************
    # process
    onProcessDetached = pyqtSignal(list, name='onProcessDetached')
    onProcessAttached = pyqtSignal(list, name='onProcessAttached')

    # script related
    onScriptLoaded = pyqtSignal(name='onScriptLoaded')
    onScriptDestroyed = pyqtSignal(name='onScriptDestroyed')
    # hook related
    onAddNativeHook = pyqtSignal(Hook, name='onAddNativeHook')
    onAddJavaHook = pyqtSignal(Hook, name='onAddJavaHook')
    onAddNativeOnLoadHook = pyqtSignal(Hook, name='onAddNativeOnLoadHook')
    onAddJavaOnLoadHook = pyqtSignal(Hook, name='onAddJavaOnLoadHook')
    onDeleteHook = pyqtSignal(list, name='onDeleteHook')
    onHitNativeOnLoad = pyqtSignal(list, name='onHitNativeOnLoad')
    onHitJavaOnLoad = pyqtSignal(str, name='onHitJavaOnLoad')
    # watcher related
    onWatcherAdded = pyqtSignal(str, int, name='onWatcherAdded')
    onWatcherRemoved = pyqtSignal(str, name='onWatcherRemoved')
    # ranges + modules
    onSetRanges = pyqtSignal(list, name='onSetRanges')
    onSetModules = pyqtSignal(list, name='onSetModules')
    onLogToConsole = pyqtSignal(str, name='onLogToConsole')
    onLogEvent = pyqtSignal(str, name='onLogEvent')
    # thread+context
    onThreadResumed = pyqtSignal(int, name='onThreadResumed')
    onRequestJsThreadResume = pyqtSignal(int, name='onRequestJsThreadResume')
    onApplyContext = pyqtSignal(dict, name='onApplyContext')
    # java
    onEnumerateJavaClassesStart = pyqtSignal(name='onEnumerateJavaClassesStart')
    onEnumerateJavaClassesMatch = pyqtSignal(str, name='onEnumerateJavaClassesMatch')
    onEnumerateJavaClassesComplete = pyqtSignal(name='onEnumerateJavaClassesComplete')
    onEnumerateJavaMethodsComplete = pyqtSignal(list, name='onEnumerateJavaMethodsComplete')
    # trace
    onJavaTraceEvent = pyqtSignal(list, name='onJavaTraceEvent')
    onSetData = pyqtSignal(list, name='onSetData')

    onBackTrace = pyqtSignal(dict, name='onBackTrace')

    onMemoryScanResult = pyqtSignal(list, name='onMemoryScanResult')

    onContextChanged = pyqtSignal(str, str, name='onContextChanged')

    # ************************************************************************
    # **************************** Init **************************************
    # ************************************************************************
    def __init__(self, session=None, parent=None, device=None):
        super(Dwarf, self).__init__(parent=parent)

        self._app_window = parent

        self.java_available = False

        # plugins
        self._plugins = []

        # frida device
        self._device = device

        # process
        self._pid = 0
        self._package = None
        self._process = None
        self._script = None
        self._spawned = False
        self._resumed = False

        # kernel
        self._kernel = Kernel(self)

        # hooks
        self.hooks = {}
        self.native_on_loads = {}
        self.java_on_loads = {}
        self.java_hooks = {}
        self.watchers = {}
        self.temporary_input = ''
        self.native_pending_args = None
        self.java_pending_args = None

        # context
        self._arch = ''
        self._pointer_size = 0
        self.contexts = {}
        self.context_tid = 0
        self._platform = ''

        # connect to self
        self.onApplyContext.connect(self._on_apply_context)
        self.onRequestJsThreadResume.connect(self._on_request_resume_from_js)

        self.keystone_installed = False
        try:
            import keystone.keystone_const
            self.keystone_installed = True
        except:
            pass

        self.reload_plugins()

    def reinitialize(self):
        self._plugins = []

        self._pid = 0
        self._package = None
        self._process = None
        self._script = None
        self._spawned = False
        self._resumed = False

        self.java_available = False

        # frida device
        self._device = None

        # process
        self._process = None
        self._script = None

        # hooks
        self.hooks = {}
        self.native_on_loads = {}
        self.java_on_loads = {}
        self.java_hooks = {}
        self.temporary_input = ''
        self.native_pending_args = None
        self.java_pending_args = None

        self.context_tid = 0

        self.reload_plugins()

    # ************************************************************************
    # **************************** Properties ********************************
    # ************************************************************************
    @property
    def kernel(self):
        return self._kernel

    @property
    def arch(self):
        return self._arch

    @property
    def pid(self):
        return self._pid

    @property
    def platform(self):
        return self._platform

    @property
    def pointer_size(self):
        return self._pointer_size

    @property
    def process(self):
        return self._process

    @property
    def device(self):
        return self._device

    @device.setter
    def device(self, value):
        try:
            if isinstance(value, frida.core.Device):
                self._device = value
        except ValueError:
            self._device = None

    @property
    def resumed(self):
        return self._resumed is True

    def current_context(self):
        key = str(self.context_tid)
        if key in self.contexts:
            return self.contexts[key]
        return None

    # ************************************************************************
    # **************************** Functions *********************************
    # ************************************************************************
    def is_address_watched(self, ptr):
        ptr = utils.parse_ptr(ptr)
        if hex(ptr) in self.watchers:
            return True

        return False

    def attach(self, pid, script=None, print_debug_error=True):
        """ Attach to pid
        """
        if self.device is None:
            raise self.NoDeviceAssignedError('No Device assigned')

        if self._process is not None:
            self.detach()

        was_error = False
        error_msg = ''

        # for commandline arg
        if isinstance(pid, str):
            try:
                process = self.device.get_process(pid)
                pid = [process.pid, process.name]
            except frida.ProcessNotFoundError as error:
                raise Exception('Frida Error: ' + str(error))

        if not isinstance(pid, list):
            raise Exception('Error pid!=list')

        try:
            self._process = self.device.attach(pid[0])
            self._process.on('detached', self._on_detached)
            self._pid = pid[0]
        except frida.ProcessNotFoundError:
            error_msg = 'Process not found (ProcessNotFoundError)'
            was_error = True
        except frida.ProcessNotRespondingError:
            error_msg = 'Process not responding (ProcessNotRespondingError)'
            was_error = True
        except frida.TimedOutError:
            error_msg = 'Frida timeout (TimedOutError)'
            was_error = True
        except frida.ServerNotRunningError:
            error_msg = 'Frida not running (ServerNotRunningError)'
            was_error = True
        except frida.TransportError:
            error_msg = 'Frida timeout was reached (TransportError)'
            was_error = True
        # keep for debug
        except Exception as error:  # pylint: disable=broad-except
            error_msg = error
            was_error = True

        if was_error:
            raise Exception(error_msg)

        self.onProcessAttached.emit([self.pid, pid[1]])
        self.load_script(script)

    def detach(self):
        if self._script is not None:
            self.dwarf_api('_detach')
            self._script.unload()
        if self._process is not None:
            self._process.detach()
            if self._spawned:
                try:
                    self.device.kill(self.pid)
                except frida.ProcessNotFoundError:
                    pass

    def load_script(self, script=None, spawned=False):
        try:
            if not os.path.exists('lib/core.js'):
                raise self.CoreScriptNotFoundError('core.js not found!')

            with open('lib/core.js', 'r') as core_script:
                script_content = core_script.read()

            self._script = self._process.create_script(script_content, runtime='v8')
            self._script.on('message', self._on_message)
            self._script.on('destroyed', self._on_script_destroyed)
            self._script.load()

            break_start = self._app_window.dwarf_args.break_start
            is_debug = self._app_window.dwarf_args.debug_script
            self._script.exports.init(break_start, is_debug, spawned)

            if script is not None:
                if os.path.exists(script):
                    with open(script, 'r') as script_file:
                        user_script = script_file.read()

                    self.dwarf_api('evaluateFunction', user_script)

            # resume immediately
            self.resume_proc()

            self.onScriptLoaded.emit()

            for plugin in self._plugins:
                try:
                    plugin.on_target_attached(self, self.pid)
                except Exception as e:
                    print('failed to dispatch target attached callback to plugin %s:\n%s' % (str(plugin), str(e)))
            return 0
        except frida.ProcessNotFoundError:
            error_msg = 'Process not found (ProcessNotFoundError)'
            was_error = True
        except frida.ProcessNotRespondingError:
            error_msg = 'Process not responding (ProcessNotRespondingError)'
            was_error = True
        except frida.TimedOutError:
            error_msg = 'Frida timeout (TimedOutError)'
            was_error = True
        except frida.ServerNotRunningError:
            error_msg = 'Frida not running (ServerNotRunningError)'
            was_error = True
        except frida.TransportError:
            error_msg = 'Frida timeout was reached (TransportError)'
            was_error = True

        if was_error:
            utils.show_message_box(error_msg)
            self._on_destroyed()
        return 1

    def spawn(self, package, script=None):
        if self.device is None:
            raise self.NoDeviceAssignedError('No Device assigned')

        if self._process is not None:
            self.detach()
        try:
            self._pid = self.device.spawn(package)
            self._package = package
            self._process = self.device.attach(self._pid)
            self._process.on('detached', self._on_detached)
            self._spawned = True
        except Exception as e:
            raise Exception('Frida Error: ' + str(e))

        self.onProcessAttached.emit([self.pid, package])
        self.load_script(script, spawned=True)

    def resume_proc(self):
        if self._spawned and not self._resumed:
            self._resumed = True
            try:
                self.device.resume(self._pid)
            except frida.InvalidOperationError:
                # already resumed from other loc
                pass

    def add_watcher(self, ptr=None):
        if ptr is None:
            ptr, input = InputDialog.input_pointer(self._app_window)
            if ptr == 0:
                return
        return self.dwarf_api('addWatcher', ptr)

    def dump_memory(self, file_path=None, ptr=0, length=0):
        if ptr == 0:
            ptr, inp = InputDialog.input_pointer(self._app_window)
        if ptr > 0:
            if length == 0:
                accept, length = InputDialog.input(
                    self._app_window, hint='insert length', placeholder='1024')
                if not accept:
                    return
                try:
                    if length.startswith('0x'):
                        length = int(length, 16)
                    else:
                        length = int(length)
                except:
                    return
            if file_path is None:
                r = QFileDialog.getSaveFileName(self._app_window, caption='Save binary dump to file')
                if len(r) == 0 or len(r[0]) == 0:
                    return
                file_path = r[0]
            data = self.read_memory(ptr, length)
            if data is not None:
                with open(file_path, 'wb') as f:
                    f.write(data)

    def dwarf_api(self, api, args=None, tid=0):
        if self.pid and self._pid == 0 or self.process is None:
            return

        # when tid is 0 we want to execute the api in the current hooked thread
        # however, when we release from menu, what we want to do is to release multiple contexts at once
        # so that we pass 0 as tid.
        # we check here and setup special rules for release api
        is_releasing = api == 'release'
        if not is_releasing and tid == 0:
            tid = self.context_tid

        if args is not None and not isinstance(args, list):
            args = [args]
        if self._script is None:
            return None
        try:
            if tid == 0:
                for tid in list(self.contexts.keys()):
                    self._script.post({"type": tid})
                    if is_releasing:
                        self._script.exports.api(int(tid), api, [int(tid)])
                if is_releasing:
                    return None
            else:
                self._script.post({"type": str(tid)})
            return self._script.exports.api(tid, api, args)
        except Exception as e:
            self.log_event(str(e))
            return None

    def hook_java(self, input_=None, pending_args=None):
        if input_ is None or not isinstance(input_, str):
            accept, input_ = InputDialog.input(
                self._app_window, hint='insert java class or method',
                placeholder='com.package.class or com.package.class.method')
            if not accept:
                return
        self.java_pending_args = pending_args
        input_ = input_.replace(' ', '')
        self.dwarf_api('hookJava', input_)

    def hook_native(self, input_=None, pending_args=None, own_input=None):
        if input_ is None or not isinstance(input_, str):
            ptr, input_ = InputDialog.input_pointer(self._app_window)
        else:
            ptr = utils.parse_ptr(self._app_window.dwarf.dwarf_api('evaluatePtr', input_))
        if ptr > 0:
            self.temporary_input = input_
            if own_input is not None:
                self.temporary_input = own_input
            self.native_pending_args = pending_args
            self.dwarf_api('hookNative', ptr)

    def hook_native_on_load(self, input_=None):
        if input_ is None or not isinstance(input_, str):
            accept, input_ = InputDialog.input(self._app_window, hint='insert module name', placeholder='libtarget.so')
            if not accept:
                return
            if len(input_) == 0:
                return

        if input_ in self._app_window.dwarf.native_on_loads:
            return

        self.dwarf_api('hookNativeOnLoad', input_)

    def hook_java_on_load(self, input_=None):
        if input_ is None or not isinstance(input_, str):
            accept, input_ = InputDialog.input(
                self._app_window, hint='insert class name', placeholder='com.android.mytargetclass')
            if not accept:
                return
            if len(input_) == 0:
                return

        if input_ in self._app_window.dwarf.native_on_loads:
            return

        self.dwarf_api('hookJavaOnLoad', input_)

    def log(self, what):
        self.onLogToConsole.emit(str(what))

    def log_event(self, what):
        self.onLogEvent.emit(str(what))

    def read_memory(self, ptr, length):
        if length > 1024 * 1024:
            position = 0
            next_size = 1024 * 1024
            data = bytearray()
            while True:
                try:
                    data += self.dwarf_api('readBytes', [ptr + position, next_size])
                except:
                    return None
                position += next_size
                diff = length - position
                if diff > 1024 * 1024:
                    next_size = 1024 * 1024
                elif diff > 0:
                    next_size = diff
                else:
                    break
            ret = bytes(data)
            del data
            return ret
        else:
            return self.dwarf_api('readBytes', [ptr, length])

    def remove_watcher(self, ptr):
        return self.dwarf_api('removeWatcher', ptr)

    def search(self, start, size, pattern):
        # sanify args
        start = utils.parse_ptr(start)
        size = int(size)
        # convert to frida accepted pattern
        pattern = ' '.join([pattern[i:i + 2] for i in range(0, len(pattern), 2)])
        self.dwarf_api('memoryScan', [start, size, pattern])

    def search_list(self, ranges_list, pattern):
        pattern = ' '.join([pattern[i:i + 2] for i in range(0, len(pattern), 2)])
        self.dwarf_api('memoryScanList', [json.dumps(ranges_list), pattern])

    # ************************************************************************
    # **************************** Handlers **********************************
    # ************************************************************************
    def _on_detached(self, process, reason, crash_log):
        self.onProcessDetached.emit([process, reason, crash_log])

    def _on_script_destroyed(self):
        self._script = None

    def _on_message(self, message, data):
        QApplication.processEvents()
        if 'payload' not in message:
            print('payload: ' + str(message))
            return

        what = message['payload']
        parts = what.split(':::')
        if len(parts) < 2:
            print(what)
            return

        cmd = parts[0]
        if cmd == 'api_ping_timeout':
            self._script.post({"type": str(parts[1])})
        elif cmd == 'backtrace':
            self.onBackTrace.emit(json.loads(parts[1]))
        elif cmd == 'class_loader_loading_class':
            str_fmt = ('@thread {0} loading class := {1}'.format(parts[1], parts[2]))
            self.log_event(str_fmt)
        elif cmd == 'enumerate_java_classes_start':
            self.onEnumerateJavaClassesStart.emit()
        elif cmd == 'enumerate_java_classes_match':
            self.onEnumerateJavaClassesMatch.emit(parts[1])
        elif cmd == 'enumerate_java_classes_complete':
            self.onEnumerateJavaClassesComplete.emit()
        elif cmd == 'enumerate_java_methods_complete':
            self.onEnumerateJavaMethodsComplete.emit([parts[1], json.loads(parts[2])])
        elif cmd == 'ftrace':
            if self.app.get_ftrace_panel() is not None:
                self.app.get_ftrace_panel().append_data(parts[1])
        elif cmd == 'enable_kernel':
            self._app_window.get_menu().enable_kernel_menu()
        elif cmd == 'hook_java_callback':
            h = Hook(HOOK_JAVA)
            h.set_ptr(1)
            h.set_input(parts[1])
            if self.java_pending_args:
                h.set_condition(self.java_pending_args['condition'])
                h.set_logic(self.java_pending_args['logic'])
                self.java_pending_args = None
            self.java_hooks[h.get_input()] = h
            self.onAddJavaHook.emit(h)
        elif cmd == 'hook_java_on_load_callback':
            h = Hook(HOOK_JAVA)
            h.set_ptr(0)
            h.set_input(parts[1])
            self.java_on_loads[parts[1]] = h
            self.onAddJavaOnLoadHook.emit(h)
        elif cmd == 'hook_native_callback':
            h = Hook(HOOK_NATIVE)
            h.set_ptr(int(parts[1], 16))
            h.set_input(self.temporary_input)
            h.set_bytes(binascii.unhexlify(parts[2]))
            self.temporary_input = ''
            h.set_condition(parts[4])
            h.set_logic(parts[3])
            h.internalHook = parts[5] == 'true'
            h.set_debug_symbol(json.loads(parts[6]))
            self.native_pending_args = None
            if not h.internal_hook:
                self.hooks[h.get_ptr()] = h
                self.onAddNativeHook.emit(h)
        elif cmd == 'hook_native_on_load_callback':
            h = Hook(HOOK_ONLOAD)
            h.set_ptr(0)
            h.set_input(parts[1])
            self.native_on_loads[parts[1]] = h
            self.onAddNativeOnLoadHook.emit(h)
        elif cmd == 'hook_deleted':
            if parts[1] == 'java':
                self.java_hooks.pop(parts[2])
            elif parts[1] == 'native_on_load':
                self.native_on_loads.pop(parts[2])
            elif parts[1] == 'java_on_load':
                self.java_on_loads.pop(parts[2])
            else:
                self.hooks.pop(utils.parse_ptr(parts[2]))
            self.onDeleteHook.emit(parts)
        elif cmd == 'java_on_load_callback':
            str_fmt = ('Hook java onload {0} @thread := {1}'.format(parts[1], parts[2]))
            self.log_event(str_fmt)
            self.onHitJavaOnLoad.emit(parts[1])
        elif cmd == 'java_trace':
            self.onJavaTraceEvent.emit(parts)
        elif cmd == 'log':
            self.log(parts[1])
        elif cmd == 'native_on_load_callback':
            str_fmt = ('Hook native onload {0} @thread := {1}'.format(parts[1], parts[3]))
            self.log_event(str_fmt)
            self.onHitNativeOnLoad.emit([parts[1], parts[2]])
        elif cmd == 'native_on_load_module_loading':
            str_fmt = ('@thread {0} loading module := {1}'.format(parts[1], parts[2]))
            self.log_event(str_fmt)
        elif cmd == 'new_thread':
            str_fmt = ('@thread {0} starting new thread with target fn := {1}'.format(parts[1], parts[2]))
            self.log_event(str_fmt)
        elif cmd == 'release':
            str_fmt = ('releasing := {0}'.format(parts[1]))
            self.log_event(str_fmt)
            if parts[1] in self.contexts:
                del self.contexts[parts[1]]
            self.onThreadResumed.emit(int(parts[1]))
        elif cmd == 'resume':
            if not self.resumed:
                self.resume_proc()
        elif cmd == 'release_js':
            # releasing the thread must be done by calling py funct dwarf_api('release')
            # there are cases in which we want to release the thread from a js api so we need to call this
            self.onRequestJsThreadResume.emit(int(parts[1]))
        elif cmd == 'set_context':
            data = json.loads(parts[1])
            if 'modules' in data:
                self.onSetModules.emit(data['modules'])
            if 'ranges' in data:
                self.onSetRanges.emit(data['ranges'])
            if 'backtrace' in data:
                self.onBackTrace.emit(data['backtrace'])

            self.onApplyContext.emit(data)
        elif cmd == 'set_context_value':
            context_property = parts[1]
            value = parts[2]
            self.onContextChanged.emit(str(context_property), value)
        elif cmd == 'set_data':
            if data:
                self.onSetData.emit(['raw', parts[1], data])
            else:
                self.onSetData.emit(['plain', parts[1], str(parts[2])])
        elif cmd == 'unhandled_exception':
            # todo
            pass
        elif cmd == 'update_modules':
            modules = json.loads(parts[2])
            # todo update onloads bases
            self.onSetModules.emit(modules)
        elif cmd == 'update_ranges':
            self.onSetRanges.emit(json.loads(parts[2]))
        elif cmd == 'watcher':
            exception = json.loads(parts[1])
            self.log_event('watcher hit op %s address %s @thread := %s' %
                     (exception['memory']['operation'], exception['memory']['address'], parts[2]))
        elif cmd == 'watcher_added':
            ptr = utils.parse_ptr(parts[1])
            hex_ptr = hex(ptr)
            flags = int(parts[2])

            h = Hook(HOOK_WATCHER)
            h.set_ptr(ptr)
            h.set_logic(flags)
            h.set_debug_symbol(json.loads(parts[3]))
            self.watchers[hex_ptr] = h

            self.onWatcherAdded.emit(hex_ptr, flags)
        elif cmd == 'watcher_removed':
            hex_ptr = hex(utils.parse_ptr(parts[1]))
            self.watchers.pop(hex_ptr)
            self.onWatcherRemoved.emit(hex_ptr)
        elif cmd == 'memoryscan_result':
            if parts[1] == '':
                self.onMemoryScanResult.emit([])
            else:
                self.onMemoryScanResult.emit(json.loads(parts[1]))
        else:
            print('unknown message: ' + what)

    def _on_apply_context(self, context_data):
        reason = context_data['reason']
        if reason == -1:
            # set initial context
            self._arch = context_data['arch']
            self._platform = context_data['platform']
            self._pointer_size = context_data['pointerSize']
            self.java_available = context_data['java']
            str_fmt = ('injected into := {0:d}'.format(self.pid))
            self.log_event(str_fmt)

            # unlock java on loads
            if self.java_available:
                self._app_window.hooks_panel.new_menu.addAction(
                    'Java class loading', self._app_window.hooks_panel._on_add_java_on_load)
        elif 'context' in context_data:
            context = Context(context_data['context'])
            self.contexts[str(context_data['tid'])] = context

            sym = ''
            if 'pc' in context_data['context']:
                name = context_data['ptr']
                if 'symbol' in context_data['context']['pc'] and \
                        context_data['context']['pc']['symbol']['name'] is not None:
                    sym = context_data['context']['pc']['symbol']['moduleName']
                    sym += ' - '
                    sym += context_data['context']['pc']['symbol']['name']
            else:
                name = context_data['ptr']

            if context_data['reason'] == 0:
                self.log_event('hook %s %s @thread := %d' % (name, sym, context_data['tid']))

        if not reason == -1 and self.context_tid == 0:
            self.context_tid = context_data['tid']

    def _on_request_resume_from_js(self, tid):
        self.dwarf_api('release', tid, tid=tid)

    def restart_proc(self):
        session = self.dump_session()
        self.reinitialize()
        self._app_window.session_manager._session = None
        self._app_window.session_stopped()

        self._restore_session(session)

    def dump_session(self):
        return {
            'session': self._app_window.session_manager.session.session_type,
            'package': self._package,
            'user_script': self._app_window.console_panel.get_js_console().function_content
        }

    def reload_plugins(self):
        plugins_path = os.path.join(os.path.sep.join(os.path.realpath(__file__).split(os.path.sep)[:-2]), 'plugins')
        for _, directories, _ in os.walk(plugins_path):
            for directory in [x for x in directories if x != '__pycache__']:
                plugin_dir = os.path.join(plugins_path, directory)
                plugin_file = os.path.join(plugin_dir, 'plugin.py')
                # check if {pluginname}.py exitsts
                if plugin_file and os.path.exists(plugin_file):
                    spec = importlib.util.spec_from_file_location('', location=plugin_file)
                    if not spec:
                        continue

                    try:
                        plugin = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(plugin)
                        plugin.init(self)
                        self._plugins.append(plugin)
                    except Exception as e:
                        print('failed to load plugin %s: %s' % (plugin_file, str(e)))
                        return
