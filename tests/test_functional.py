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
import subprocess
import sys

import fixtures
import testtools
from testtools import content


class _FunctionalBase(object):
    def setUp(self):
        super(_FunctionalBase, self).setUp()
        tmpdir = self.useFixture(fixtures.TempDir()).path
        self.config_file = os.path.join(tmpdir, 'rootwrap.conf')
        filters_dir = os.path.join(tmpdir, 'filters.d')
        filters_file = os.path.join(tmpdir, 'filters.d', 'test.filters')
        os.mkdir(filters_dir)
        with open(self.config_file, 'w') as f:
            f.write("""[DEFAULT]
filters_path=%s
exec_dirs=/bin""" % (filters_dir,))
        with open(filters_file, 'w') as f:
            f.write("""[Filters]
echo: CommandFilter, /bin/echo, root
cat: CommandFilter, /bin/cat, root
""")

    def test_run_once(self):
        code, out, err = self.execute(['echo', 'teststr'])
        self.assertEqual(0, code)
        self.assertEqual(b'teststr\n', out)
        self.assertEqual(b'', err)

    def test_run_with_stdin(self):
        code, out, err = self.execute(['cat'], stdin=b'teststr')
        self.assertEqual(0, code)
        self.assertEqual(b'teststr', out)
        self.assertEqual(b'', err)


class RootwrapTest(_FunctionalBase, testtools.TestCase):
    def setUp(self):
        super(RootwrapTest, self).setUp()
        self.cmd = [
            sys.executable, '-c',
            'from oslo.rootwrap import cmd; cmd.main()',
            self.config_file]

    def execute(self, cmd, stdin=None):
        proc = subprocess.Popen(
            self.cmd + cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(stdin)
        self.addDetail('stdout',
                       content.text_content(out.decode('utf-8', 'replace')))
        self.addDetail('stderr',
                       content.text_content(err.decode('utf-8', 'replace')))
        return proc.returncode, out, err
