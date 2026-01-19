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

import os
import pwd

try:
    import eventlet
except ImportError:
    eventlet = None

import fixtures
import testtools

from oslo_rootwrap import cmd


class _FunctionalBase(testtools.TestCase):
    def setUp(self) -> None:
        super().setUp()
        tmpdir = self.useFixture(fixtures.TempDir()).path
        self.config_file = os.path.join(tmpdir, 'rootwrap.conf')
        self.later_cmd = os.path.join(tmpdir, 'later_install_cmd')
        filters_dir = os.path.join(tmpdir, 'filters.d')
        filters_file = os.path.join(tmpdir, 'filters.d', 'test.filters')
        os.mkdir(filters_dir)
        with open(self.config_file, 'w') as f:
            f.write(f"""[DEFAULT]
filters_path={filters_dir}
daemon_timeout=10
exec_dirs=/bin""")
        with open(filters_file, 'w') as f:
            f.write(f"""[Filters]
echo: CommandFilter, /bin/echo, root
cat: CommandFilter, /bin/cat, root
sh: CommandFilter, /bin/sh, root
id: CommandFilter, /usr/bin/id, nobody
unknown_cmd: CommandFilter, /unknown/unknown_cmd, root
later_install_cmd: CommandFilter, {self.later_cmd}, root
""")

    def _test_run_once(self, expect_byte: bool = True) -> None:
        code, out, err = self.execute(['echo', 'teststr'])
        self.assertEqual(0, code)
        expect_out: str | bytes
        expect_err: str | bytes
        if expect_byte:
            expect_out = b'teststr\n'
            expect_err = b''
        else:
            expect_out = 'teststr\n'
            expect_err = ''
        self.assertEqual(expect_out, out)
        self.assertEqual(expect_err, err)

    def _test_run_with_stdin(self, expect_byte: bool = True) -> None:
        code, out, err = self.execute(['cat'], stdin=b'teststr')
        self.assertEqual(0, code)
        expect_out: str | bytes
        expect_err: str | bytes
        if expect_byte:
            expect_out = b'teststr'
            expect_err = b''
        else:
            expect_out = 'teststr'
            expect_err = ''
        self.assertEqual(expect_out, out)
        self.assertEqual(expect_err, err)

    def test_run_with_path(self):
        code, out, err = self.execute(['/bin/echo', 'teststr'])
        self.assertEqual(0, code)

    def test_run_with_bogus_path(self):
        code, out, err = self.execute(['/home/bob/bin/echo', 'teststr'])
        self.assertEqual(cmd.RC_UNAUTHORIZED, code)

    def test_run_command_not_found(self):
        code, out, err = self.execute(['unknown_cmd'])
        self.assertEqual(cmd.RC_NOEXECFOUND, code)

    def test_run_unauthorized_command(self):
        code, out, err = self.execute(['unauthorized_cmd'])
        self.assertEqual(cmd.RC_UNAUTHORIZED, code)

    def test_run_as(self):
        if os.getuid() != 0:
            self.skip('Test requires root (for setuid)')

        # Should run as 'nobody'
        code, out, err = self.execute(['id', '-u'])
        self.assertEqual(pwd.getpwnam('nobody').pw_uid, int(out.strip()))

        # Should run as 'root'
        code, out, err = self.execute(['sh', '-c', 'id -u'])
        self.assertEqual(0, int(out.strip()))
