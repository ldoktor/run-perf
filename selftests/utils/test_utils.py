#!/bin/env python3
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2020
# Author: Lukas Doktor <ldoktor@redhat.com>

import os
import re
import tempfile
from unittest import mock
import unittest

from runperf import utils
import shutil
import contextlib


class BasicUtils(unittest.TestCase):

    def test_list_of_threads(self):
        self.assertRaises(ValueError, utils.list_of_threads, -5)
        self.assertRaises(ValueError, utils.list_of_threads, 0)
        self.assertEqual(utils.list_of_threads(1), "1")
        self.assertEqual(utils.list_of_threads(2), "1,2")
        self.assertEqual(utils.list_of_threads(7), "1,2,3,4,5,6,7")
        self.assertEqual(utils.list_of_threads(18), "1,4,8,12,16,18")
        self.assertEqual(utils.list_of_threads(79), "1,19,38,57,76,79")
        self.assertEqual(utils.list_of_threads(2048), "1,512,1024,1536,2048")

    def test_read_write_file(self):
        with tempfile.TemporaryDirectory(prefix="runperf-") as tmpdir:
            path = os.path.join(tmpdir, "dir", "file")
            self.assertEqual(-1, utils.read_file(path))
            utils.write_file(path, "foo")
            self.assertEqual("foo", utils.read_file(path))

    def test_comma_separated_ranges_to_list(self):
        text = ""
        self.assertEqual([], utils.comma_separated_ranges_to_list(text))
        text = "0,1-2,10,15-17"
        self.assertEqual([0, 1, 2, 10, 15, 16, 17],
                         utils.comma_separated_ranges_to_list(text))

    def test_random_string(self):
        out = utils.random_string(10)
        self.assertEqual(10, len(out))

    def test_check_output(self):
        self.assertEqual("aaa\n", utils.check_output(["echo", "aaa"]))
        self.assertEqual("aaa\n", utils.check_output("echo aaa", shell=True))
        # read on closed stdin should return immediately (otherwise it hangs)
        self.assertEqual("", utils.check_output(["cat", "/dev/stdin"]))

    def test_wait_for(self):
        with mock.patch("time.time", mock.Mock(side_effect=[0, 0, 0])):
            func = mock.Mock(side_effect=[0, 1])
            ret = utils.wait_for(func, 1)
            self.assertEqual(True, ret)
        with mock.patch("time.time", mock.Mock(side_effect=[0, 0, 1])):
            ret = utils.wait_for(lambda: False, 1)
            self.assertEqual(None, ret)

    def test_tabular_output(self):
        self.assertEqual("", utils.tabular_output([]))
        self.assertEqual("b", utils.tabular_output([['b'], []]))
        self.assertEqual('a   aa aaa\nbbb bb b',
                         utils.tabular_output([["a", "aa", "aaa"],
                                               ["bbb", "bb", "b"]]))
        self.assertEqual('    HEADER\na   aa     aaa\nbbb bb',
                         utils.tabular_output([["a", "aa", "aaa"],
                                               ["bbb", "bb"]],
                                              ["", "HEADER"]))

    def test_string_to_safe_path(self):
        stsp = utils.string_to_safe_path
        self.assertEqual("", stsp(""))
        self.assertEqual("_name", stsp(".name"))
        self.assertEqual("a" * 255, stsp("a" * 300))
        self.assertEqual("_n_a_m_e_", stsp(".n<a>m:e\""))
        # each \u4500 turns into 3 characters
        self.assertEqual('_' * 255, stsp("\u4500"*100))

    def test_shell_write_content_cmd(self):
        swcc = utils.shell_write_content_cmd
        match = re.match(r'cat > \'\'"\'"\'"/path/to/file\' << \\([^\n]+)\n'
                         r'some\nmultiline\ntext\n([^\n]+)',
                         swcc("'\"/path/to/file", "some\nmultiline\ntext"))
        self.assertNotEqual(match, None)
        self.assertEqual(match[1], match[2])
        match = re.match(r'cat >> \'\'"\'"\'"/path/to/file\' << \\([^\n]+)\n'
                         r'some\nmultiline\ntext\n([^\n]+)',
                         swcc("'\"/path/to/file", "some\nmultiline\ntext",
                              True))
        self.assertNotEqual(match, None)
        self.assertEqual(match[1], match[2])

    def test_entry_points(self):
        class EP:
            def __init__(self, name=None, loaded_name=None):
                self.name = name
                self.loaded_name = loaded_name
            def load(self):
                plugin = mock.Mock()
                plugin.name = self.loaded_name
                plugin.plugin = self.name
                return plugin
        entries = lambda _: [EP("10"), EP("20"), EP("30")]
        with mock.patch("runperf.utils.pkg_resources.iter_entry_points",
                        entries):
            self.assertEqual(["10", "20", "30"],
                             [_.name for _ in utils.sorted_entry_points('')])
        entries = lambda _: [EP("20"), EP("30"), EP("10")]
        with mock.patch("runperf.utils.pkg_resources.iter_entry_points",
                        entries):
            self.assertEqual(["10", "20", "30"],
                             [_.name for _ in utils.sorted_entry_points('')])
        entries = lambda _: [EP("20", "foo"), EP("30", "foo"), EP("10", "bar")]
        with mock.patch("runperf.utils.pkg_resources.iter_entry_points",
                        entries):
            act = utils.named_entry_point("", "foo")
            self.assertEqual(("foo", "20"), (act.name, act.plugin))
        entries = lambda _: [EP("30", "foo"), EP("20", "foo"), EP("10", "bar")]
        with mock.patch("runperf.utils.pkg_resources.iter_entry_points",
                        entries):
            act = utils.named_entry_point("", "foo")
            self.assertEqual(("foo", "20"), (act.name, act.plugin))
        with mock.patch("runperf.utils.pkg_resources.iter_entry_points",
                        entries):
            self.assertRaises(KeyError, utils.named_entry_point, "", "missing")

class Machine:
    def __init__(self, name, sessions=None):
        self.name = name
        self.sessions = sessions

    def get_fullname(self):
        return self.name

    def copy_from(self, src, dst):
        shutil.copytree(src, dst)

    @contextlib.contextmanager
    def get_session_cont(self):
        for session in self.sessions:
            yield session

class LogFetcher(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="runperf-selftest")

    def test_default(self):
        fetcher = utils.LogFetcher()
        os.makedirs(os.path.join(self.tmpdir, 'TO', 'BE', 'COLLECTED'))
        path1 = os.path.join(self.tmpdir, 'TO', 'some')
        with open(path1, 'w') as fd_tmp:
            fd_tmp.write('SOME')
        path2 = os.path.join(self.tmpdir, 'TO', 'BE', 'files')
        with open(path2, 'w') as fd_tmp:
            fd_tmp.write('CONTENT')
        path3 = os.path.join(self.tmpdir, 'TO', 'BE', 'COLLECTED', 'we_want')
        with open(path3, 'w') as fd_tmp:
            fd_tmp.write('LAST')
        fetcher.paths.add(os.path.join(self.tmpdir, 'TO'))
        session = mock.Mock()
        session.cmd_output.return_value = 'OUTPUT'
        machine = Machine('vm1', [session])
        fetcher.collect(self.tmpdir, machine)
        prefix = os.path.join(self.tmpdir, 'vm1') + '/'
        with open(prefix + path1) as fd_tmp:
            self.assertEqual('SOME', fd_tmp.read())
        with open(prefix + path2) as fd_tmp:
            self.assertEqual('CONTENT', fd_tmp.read())
        with open(prefix + path3) as fd_tmp:
            self.assertEqual('LAST', fd_tmp.read())
        with open(prefix + 'COMMANDS/journalctl --no-pager '
                  '--since=@%(since)s') as fd_tmp:
            self.assertEqual('OUTPUT', fd_tmp.read())

    def test_fail_to_get_session(self):
        fetcher = utils.LogFetcher()
        fetcher.paths.add('/foo/bar/baz')
        machine = mock.Mock()
        machine.get_fullname.return_value = "TEST"
        fetcher.collect(self.tmpdir, machine)
        # Fetch fails so the "baz" should not be created
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'TEST',
                                                    'foo/bar')))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'TEST',
                                                     'foo/bar/baz')))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'TEST',
                                                    'COMMANDS')))

    def tearDown(self):
        if self.tmpdir:
            shutil.rmtree(self.tmpdir)


if __name__ == '__main__':
    unittest.main()
