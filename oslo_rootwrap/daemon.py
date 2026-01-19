# Copyright (c) 2014 Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import logging
from multiprocessing import managers
import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from types import FrameType

from oslo_rootwrap import cmd
from oslo_rootwrap import filters as filters_mod
from oslo_rootwrap import jsonrpc
from oslo_rootwrap import subprocess
from oslo_rootwrap import wrapper

LOG = logging.getLogger(__name__)

# Since multiprocessing supports only pickle and xmlrpclib for serialization of
# RPC requests and responses, we declare another 'jsonrpc' serializer

managers.listener_client['jsonrpc'] = (  # type: ignore[attr-defined]
    jsonrpc.JsonListener,
    jsonrpc.JsonClient,
)


class RootwrapClass:
    last_called: float
    daemon_timeout: int
    timeout: threading.Timer

    def __init__(
        self,
        config: wrapper.RootwrapConfig,
        filters: list[filters_mod.CommandFilter],
    ) -> None:
        self.config = config
        self.filters = filters
        self.reset_timer()
        self.prepare_timer(config)

    def run_one_command(
        self, userargs: list[str], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self.reset_timer()
        try:
            obj = wrapper.start_subprocess(
                self.filters,
                userargs,
                exec_dirs=self.config.exec_dirs,
                log=self.config.use_syslog,
                close_fds=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except wrapper.FilterMatchNotExecutable:
            LOG.warning("Executable not found for: %s", ' '.join(userargs))
            return cmd.RC_NOEXECFOUND, "", ""

        except wrapper.NoFilterMatched:
            LOG.warning(
                "Unauthorized command: %s (no filter matched)",
                ' '.join(userargs),
            )
            return cmd.RC_UNAUTHORIZED, "", ""

        stdin_bytes: bytes | None = None
        if stdin is not None:
            stdin_bytes = os.fsencode(stdin)
        out, err = obj.communicate(stdin_bytes)
        out_str = os.fsdecode(out)
        err_str = os.fsdecode(err)
        return obj.returncode or 0, out_str, err_str

    @classmethod
    def reset_timer(cls) -> None:
        cls.last_called = time.time()

    @classmethod
    def cancel_timer(cls) -> None:
        try:
            cls.timeout.cancel()
        except RuntimeError:
            pass

    @classmethod
    def prepare_timer(
        cls, config: wrapper.RootwrapConfig | None = None
    ) -> None:
        if config is not None:
            cls.daemon_timeout = config.daemon_timeout
        # Wait a bit longer to avoid rounding errors
        timeout = (
            max(cls.last_called + cls.daemon_timeout - time.time(), 0) + 1
        )
        if getattr(cls, 'timeout', None):
            # Another timer is already initialized
            return
        cls.timeout = threading.Timer(timeout, cls.handle_timeout)
        cls.timeout.start()

    @classmethod
    def handle_timeout(cls) -> None:
        if cls.last_called < time.time() - cls.daemon_timeout:
            cls.shutdown()

        cls.prepare_timer()

    @staticmethod
    def shutdown() -> None:
        # Suicide to force break of the main thread
        os.kill(os.getpid(), signal.SIGINT)


def get_manager_class(
    config: wrapper.RootwrapConfig | None = None,
    filters: list[filters_mod.CommandFilter] | None = None,
) -> type[managers.BaseManager]:
    class RootwrapManager(managers.BaseManager):
        def __init__(
            self, address: str | None = None, authkey: bytes | None = None
        ) -> None:
            # Force jsonrpc because neither pickle nor xmlrpclib is secure
            super().__init__(address, authkey, serializer='jsonrpc')

    if config is not None:
        assert filters is not None  # narrow type
        partial_class = functools.partial(RootwrapClass, config, filters)
        RootwrapManager.register('rootwrap', partial_class)
    else:
        RootwrapManager.register('rootwrap')

    return RootwrapManager


def daemon_start(
    config: wrapper.RootwrapConfig,
    filters: list[filters_mod.CommandFilter],
) -> None:
    temp_dir = tempfile.mkdtemp(prefix='rootwrap-')
    LOG.debug("Created temporary directory %s", temp_dir)
    try:
        # allow everybody to find the socket
        rwxr_xr_x = (
            stat.S_IRWXU
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
        os.chmod(temp_dir, rwxr_xr_x)
        socket_path = os.path.join(temp_dir, "rootwrap.sock")
        LOG.debug("Will listen on socket %s", socket_path)
        manager_cls = get_manager_class(config, filters)
        manager = manager_cls(address=socket_path)
        server = manager.get_server()
        try:
            # allow everybody to connect to the socket
            rw_rw_rw_ = (
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH
                | stat.S_IWOTH
            )
            os.chmod(socket_path, rw_rw_rw_)
            sys.stdout.buffer.write(socket_path.encode('utf-8'))
            sys.stdout.buffer.write(b'\n')
            # type stubs don't expose this paramter currently
            sys.stdout.buffer.write(bytes(server.authkey))  # type: ignore[attr-defined]
            sys.stdin.close()
            sys.stdout.close()
            sys.stderr.close()
            # Gracefully shutdown on INT or TERM signals
            stop = functools.partial(daemon_stop, server)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            LOG.info("Starting rootwrap daemon main loop")
            server.serve_forever()
        finally:
            conn: jsonrpc.JsonListener = server.listener  # type: ignore[attr-defined]
            # This will break accept() loop with EOFError if it was not in the
            # main thread (as in Python 3.x)
            conn.close()
            # Closing all currently connected client sockets for reading to
            # break worker threads blocked on recv()
            for cl_conn in conn.get_accepted():
                try:
                    cl_conn.half_close()
                except Exception:
                    # Most likely the socket have already been closed
                    LOG.debug("Failed to close connection")
            RootwrapClass.cancel_timer()
            LOG.info("Waiting for all client threads to finish.")
            for thread in threading.enumerate():
                if thread.daemon:
                    LOG.debug("Joining thread %s", thread)
                    thread.join()
    finally:
        LOG.debug("Removing temporary directory %s", temp_dir)
        shutil.rmtree(temp_dir)


def daemon_stop(
    server: managers.Server, signum: int, frame: FrameType | None
) -> None:
    LOG.info("Got signal %s. Shutting down server", signum)
    # Signals are caught in the main thread which means this handler will run
    # in the middle of serve_forever() loop. It will catch this exception and
    # properly return. Since all threads created by server_forever are
    # daemonic, we need to join them afterwards. In Python 3 we can just hit
    # stop_event instead.
    server.stop_event.set()  # type: ignore[attr-defined]
