# Copyright (C) 2017 by Clearcode <http://clearcode.cc>
# and associates (see AUTHORS).

# This file is part of pytest-redis.

# pytest-redis is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# pytest-redis is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with pytest-redis.  If not, see <http://www.gnu.org/licenses/>.
"""Redis executor."""
import os
import platform
import re
import socket
from itertools import islice
from pathlib import Path
from tempfile import gettempdir
from typing import Union, Any

from mirakuru import TCPExecutor
from pkg_resources import parse_version
from py.path import local

MAX_UNIXSOCKET = 104
if platform.system() == "Linux":
    MAX_UNIXSOCKET = 107


def extract_version(text: str) -> Any:
    """
    Extract version number from the text.

    :param text: text that contains the version number
    """
    matches = re.search(r"\d+(?:\.\d+)+", text)
    assert matches is not None
    return parse_version(matches.group(0))


class RedisUnsupported(Exception):
    """Exception raised when redis<2.6 would be detected."""


class RedisMisconfigured(Exception):
    """Exception raised when the redis_exec points to non existing file."""


class UnixSocketTooLong(Exception):
    """Exception raised when unixsocket path is too long."""


class NoopRedis(TCPExecutor):
    """Reddis class respsenting an instance started by a third party."""

    def __init__(
        self, host, port, username=None, password=None, unixsocket=None, startup_timeout=15
    ):
        """
        Init method of NoopRedis.

        :param str host: server's host
        :param int port: server's port
        :param str username: server's username
        :param str password: server's password
        :param int timeout: executor's timeout for start and stop actions
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.unixsocket = unixsocket
        self.timeout = startup_timeout
        super().__init__([], host, port, timeout=startup_timeout)

    def start(self):
        """Start is a NOOP."""
        self._set_timeout()
        self.wait_for(self.redis_available)
        return self

    def redis_available(self):
        """Return True if connecting to Redis is possible"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.settimeout(0.1)
                s.connect((self.host, self.port))
            except (TimeoutError, ConnectionRefusedError):
                return False
        return True


class RedisExecutor(TCPExecutor):
    """
    Reddis executor.

    Extended TCPExecutor to contain all required logic for parametrising
    and properly constructing command to start redis-server.
    """

    MIN_SUPPORTED_VERSION = parse_version("2.6")
    """
    Minimum required version of redis that is accepted by pytest-redis.
    """

    def __init__(
        self,
        executable,
        databases,
        redis_timeout,
        loglevel,
        host,
        port,
        username=None,
        password=None,
        startup_timeout=60,
        save="",
        daemonize="no",
        rdbcompression=True,
        rdbchecksum=False,
        syslog_enabled=False,
        appendonly="no",
        datadir: Union[local, Path] = None,
    ):  # pylint:disable=too-many-locals
        """
        Init method of a RedisExecutor.

        :param str executable: path to redis-server
        :param int databases: number of databases
        :param int redis_timeout: client's connection timeout
        :param str loglevel: redis log verbosity level
        :param str host: server's host
        :param int port: server's port
        :param str username: server's username
        :param str password: server's password
        :param int startup_timeout: executor's timeout for start and stop actions
        :param str log_prefix: prefix for log filename
        :param str save: redis save configuration setting
        :param str daemonize:
        :param bool rdbcompression: Compress redis dump files
        :param bool rdbchecksum: Whether to add checksum to the rdb files
        :param bool syslog_enabled: Whether to enable logging
            to the system logger
        :param datadir: location where all the process files will be located
        :param str appendonly:
        """
        if not datadir:
            datadir = Path(gettempdir())
        self.unixsocket = str(datadir / f"redis.{port}.sock")
        self.executable = executable

        self.username = username
        self.password = password

        logfile_path = datadir / f"redis-server.{port}.log"
        pidfile_path = datadir / f"redis-server.{port}.pid"

        command = [
            self.executable,
            "--daemonize",
            daemonize,
            "--rdbcompression",
            self._redis_bool(rdbcompression),
            "--rdbchecksum",
            self._redis_bool(rdbchecksum),
            "--appendonly",
            appendonly,
            "--databases",
            str(databases),
            "--timeout",
            str(redis_timeout),
            "--pidfile",
            str(pidfile_path),
            "--unixsocket",
            self.unixsocket,
            "--dbfilename",
            f"dump.{port}.rdb",
            "--logfile",
            str(logfile_path),
            "--loglevel",
            loglevel,
            "--syslog-enabled",
            self._redis_bool(syslog_enabled),
            "--bind",
            str(host),
            "--port",
            str(port),
            "--dir",
            str(datadir),
        ]
        if password:
            command.extend(["--requirepass", str(password)])
        if save:
            if self.version < parse_version("7"):
                save_parts = save.split()
                assert all(
                    (part.isdigit() for part in save_parts)
                ), "all save arguments should be numbers"
                assert (
                    len(save_parts) % 2 == 0
                ), "there should be even number of elements passed to save"
                for time, change in zip(
                    islice(save_parts, 0, None, 2), islice(save_parts, 1, None, 2)
                ):
                    command.extend([f"--save {time} {change}"])
            else:
                command.extend([f"--save {save}"])

        super().__init__(command, host, port, timeout=startup_timeout)

    @classmethod
    def _redis_bool(cls, value):
        """
        Convert the boolean value to redis's yes/no.

        :param bool value: boolean value to convert
        :returns: yes for True, no for False
        :rtype: str
        """
        return "yes" if value and value != "no" else "no"

    def start(self):
        """Check supported version before starting."""
        self._check_unixsocket_length()
        self._check_version()
        return super().start()

    def _check_unixsocket_length(self):
        """Check unixsocket length."""
        if len(self.unixsocket) > MAX_UNIXSOCKET:
            raise UnixSocketTooLong(
                f"Unix Socket path is longer than {MAX_UNIXSOCKET} "
                f"allowed on your system: {self.unixsocket}. "
                f"It's probably due to the temporary directory configuration. "
                f"You can configure that for python by changing TMPDIR envvar, "
                f"add for example `--basetemp=/tmp/pytest` to your pytest "
                f"command or add `addopts = --basetemp=/tmp/pytest` to your "
                f"pytest configuration file."
            )

    @property
    def version(self) -> Any:
        with os.popen(f"{self.executable} --version") as version_output:
            version_string = version_output.read()
        if not version_string:
            raise RedisMisconfigured(
                f"Bad path to redis_exec is given:"
                f" {self.executable} not exists or wrong program"
            )

        return extract_version(version_string)

    def _check_version(self):
        """Check redises version if it's compatible."""
        redis_version = self.version
        if redis_version < self.MIN_SUPPORTED_VERSION:
            raise RedisUnsupported(
                f"Your version of Redis is not supported. "
                f"Consider updating to Redis {self.MIN_SUPPORTED_VERSION} at least. "
                f"The currently installed version of Redis: {redis_version}."
            )
