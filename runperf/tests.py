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
# Copyright: Red Hat Inc. 2018
# Author: Lukas Doktor <ldoktor@redhat.com>

import json
import os
import tempfile
import time

from . import exceptions
from . import utils
from .utils import pbench


class BaseTest:
    """Base implementation of a Test class"""

    name = ""
    min_groups = 1

    def __init__(self, host, workers, base_output_path,
                 metadata, extra):  # pylint: disable=W0613
        self.host = host
        self.workers = workers
        if not os.path.exists(base_output_path):
            os.makedirs(base_output_path)
        self.output = tempfile.mkdtemp(prefix="tmp", dir=base_output_path)
        self.metadata = metadata

    def setup(self):
        """
        Allow extra steps before test execution
        """

    def run(self):
        """Run the testing"""
        if len(self.workers) < self.min_groups:
            msg = ("Not enough groups of workers (%s < %s)"
                   % len(self.workers), self.min_groups)
            with open(os.path.join(self.output, "SKIP"), 'w') as skip:
                skip.write(msg)
            raise exceptions.TestSkip("msg")
        return self._run()

    def _run(self):
        """
        Deploy, run and fetch results to self.output
        """
        raise NotImplementedError

    def inject_metadata(self, session, path):
        """
        Add our "RUNPERF_METADATA.json" to the dirname($path) in order to
        preserve our extended data (especially profile, workers and such...)

        :param session: Session to the worker
        :param path: Path where the results should be located
        """
        meta = {}
        for key, value in self.metadata.items():
            meta[key] = value
        meta['distro'] = self.host.distro
        meta['profile'] = self.host.profile.name
        str_workers = {}
        for i, workers in enumerate(self.workers):
            str_workers[i] = {worker.name: worker.get_info()
                              for worker in workers}
        meta['workers'] = str_workers
        dir_path = os.path.dirname(path)
        if session.cmd_status("[ -d '%s' ]" % dir_path) == 0:
            result_path = os.path.join(dir_path, "RUNPERF_METADATA.json")
            results_json = json.dumps(meta, indent=4, sort_keys=True)
            session.cmd(utils.shell_write_content_cmd(result_path,
                                                      results_json),
                        timeout=600, print_func='mute')

    def cleanup(self):
        """
        Cleanup the environment; is **always** executed even for SKIP tests
        """


class DummyTest(BaseTest):
    name = "DummyTest"

    def _run(self):
        result_path = os.path.join(self.output, "result.json")
        with open(result_path, 'w') as result:
            with open(os.path.join(os.path.dirname(__file__), "assets",
                                   "tests", "DummyTest",
                                   "result.json")) as src:
                result.write(src.read() % {"hostname": self.host.get_addr()})
        with self.host.get_session_cont() as session:
            self.inject_metadata(session, result_path)

class PBenchTest(BaseTest):
    """
    Pbench test

    Metadata: pbench_server - set the pbench-server-url
    Metadata: pbench_server_publish - publish results to pbench server
    """

    test = ""
    args = ""
    default_args = ()
    timeout = 172800

    def __init__(self, host, workers, base_output_path,
                 metadata, extra):
        super().__init__(host, workers, base_output_path, metadata, extra)
        if "pbench_server_publish" in self.metadata:
            self.pbench_publish = True
        else:
            self.pbench_publish = False
        for key, value in self.default_args:
            if key not in extra:
                extra[key] = value
        # Using sorted to always use the same cmdline
        for key, value in sorted(extra.items()):
            self.args += " --%s=%s" % (key, value)
        self._cmd = ("pbench-%s %s --clients=%s" %
                     (self.test, self.args,
                      ",".join(_.get_addr() for _ in self.workers[0])))

    def setup(self):
        def install_pbench(host, metadata, test):
            with host.get_session_cont() as session:
                pbench.install_on(session, metadata, test=test)
        install_pbench(self.host, self.metadata, self.test)
        threads = []
        if self.host in self.workers:
            # When host is also in workers, perform install first on host
            install_pbench(self.host, self.metadata, self.test)
        else:
            name = "host %s" % self.host.name
            threads.append(utils.ThreadWithStatus(target=install_pbench,
                                                  name=name,
                                                  args=(self.host,
                                                        self.metadata,
                                                        self.test)))
        for workers in self.workers:
            for worker in workers:
                name = "worker %s" % worker.name
                threads.append(utils.ThreadWithStatus(target=install_pbench,
                                                      name=name,
                                                      args=(worker,
                                                            self.metadata,
                                                            self.test)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        failed = [thread for thread in threads if thread.completed is not True]
        if failed:
            raise RuntimeError("Failed to install pbench on %s" % failed)
        # Wait for the machines to calm down before the testing and use
        # hop=self.host as the host will be executing the ssh commands.
        for workers in self.workers:
            for worker in workers:
                with worker.get_session_cont(hop=self.host) as session:
                    if not utils.wait_for_machine_calms_down(session,
                                                             timeout=1800):
                        worker.log.warning("Worker did not stabilize in 1800s,"
                                           " proceeding on a loaded machine!")
        with self.host.get_session_cont(hop=self.host) as session:
            if not utils.wait_for_machine_calms_down(session, timeout=1800):
                worker.log.warning("Host did not stabilize in 1800s,"
                                   " proceeding on a loaded machine!")

    def _run(self):
        # We only need one group of workers
        session = None
        try:
            with self.host.get_session_cont() as session:
                session.cmd("true")
                # FIXME: Return this when https://github.com/distributed
                # -system-analysis/pbench/issues/1743 is resolved
                session.cmd(". /opt/pbench-agent/base")
                # And now run the test
                benchmark_bin = utils.shell_find_command(session, self.test)
                if benchmark_bin:
                    prefix = "benchmark_bin=%s " % benchmark_bin
                else:
                    prefix = ""
                session.cmd_output(prefix + self._cmd,
                                   timeout=self.timeout)
                # Let the system to rest a bit after heavy load
                time.sleep(5)
                ret = session.cmd_output(session.status_test_command, 10)
                digit_lines = [l for l in ret.splitlines()
                               if l.strip().isdigit()]
                if digit_lines:
                    if int(digit_lines[0].strip()) != 0:
                        raise RuntimeError("Execution failed %s" % digit_lines)
                else:
                    raise RuntimeError("Failed to get status")
                src = session.cmd_output("echo $(ls -dt /var/lib/pbench-agent/"
                                         "%s__*/ | head -n 1)"
                                         % self.test).strip()
                self.inject_metadata(session, os.path.join(src, "result.json"))
                if self.pbench_publish:
                    extra_args = []
                    user = self.metadata.get("project")
                    if user:
                        extra_args.append("--user %s" % user)
                    prefix = self.metadata.get("build")
                    if prefix:
                        extra_args.append("--prefix %s" % prefix)
                    session.cmd("pbench-copy-results %s"
                                % " ".join(extra_args), timeout=600)
            self.host.copy_from(src, self.output)
        finally:
            session.close()


class PBenchFio(PBenchTest):
    """Default fio benchmark (read)"""

    name = "fio"
    test = "fio"
    default_args = (("test-types", "read,write,rw"),
                    ("ramptime", 10),
                    ("runtime", 180),
                    ("samples", 3))


class Linpack(PBenchTest):
    """linpack test"""

    name = "linpack"
    test = "linpack"
    default_args = (("run-samples", 3),)
    __detect_linpack_bin = True

    def __init__(self, host, workers, base_output_path, metadata, extra):
        if "linpack-binary" in extra:
            self._detect_linpack_bin = False
        if "threads" not in extra:
            # We want 2*cpus to stress the scheduler
            extra["threads"] = utils.list_of_threads(
                host.params["guest_cpus"] * 2)
        PBenchTest.__init__(self, host, workers, base_output_path, metadata,
                            extra)
        # Replace the PBenchTest's pbench-linpack command for
        # pbench-run-benchmark as pbench-linpack does not provides json results
        self._cmd = ("ANSIBLE_HOST_KEY_CHECKING=false "
                     "ANSIBLE_PYTHON_INTERPRETER=/usr/bin/python3 "
                     "pbench-run-benchmark %s %s"
                     % (self.test, self._cmd.split(' ', 1)[1]))

    def _run(self):
        if self.__detect_linpack_bin:
            # When linpack is not specified by the user we need to detect
            # and append it now as it was probably installed during `setup()`
            with self.host.get_session_cont() as session:
                linpack_bin = None
                for name in ("linpack", "xlinpack_xeon64"):
                    linpack_bin = utils.shell_find_command(session, name)
                    if linpack_bin:
                        break
                if not linpack_bin:
                    linpack_bin = session.cmd_output(
                        "ls /usr/local/*/benchmarks/linpack/xlinpack_xeon64 "
                        "2>/dev/null").strip()
                    if not linpack_bin:
                        raise exceptions.TestSkip("No linpack binary found on "
                                                  "host")
                    linpack_bin = linpack_bin.splitlines()[0]
                self._cmd += " --linpack-binary='%s'" % linpack_bin
        PBenchTest._run(self)


class UPerf(PBenchTest):
    """
    Uperf test

    By default executes tcp stream test. If you need to test udp we strongly
    suggest also setting type=rr, otherwise it's not guaranteed the packets
    are not plainly dropped.
    """

    name = "uperf"
    test = "uperf"
    default_args = (("test-types", "stream"),
                    ("runtime", 60),
                    ("samples", 3),
                    ("protocols", "tcp"),
                    ("message-sizes", "1,64,16384"))

    def __init__(self, host, workers, base_output_path, metadata, extra):
        super().__init__(host, workers, base_output_path, metadata, extra)
        # FIXME: Workaround missing perl paths
        self._cmd = ("PERL5LIB=/opt/pbench-agent/tool-scripts/postprocess/:"
                     "/opt/pbench-agent/bench-scripts/postprocess/ %s"
                     % self._cmd)
        # FIXME: Ugly IPv4-libvirt-bridge-only hack to use main host
        addrs = []
        for worker in self.workers[0]:
            addr = worker.get_host_addr()
            utils.ssh_copy_id(self.host.log, addr, host.default_passwords,
                              self.host)
            addrs.append(addr)
        self._cmd += (" --servers %s" % (",".join(addrs)))


class PBenchNBD(PBenchFio):
    """
    Executes PBenchFio with a custom job to test nbd

    By default it creates and distributes the job-file using "nbd-check.fio"
    from assets but you can override the job-file path and distribute your
    own version. In such case you have to make sure to use the right paths
    and format.
    """
    name = "fio-nbd"
    default_args = (("numjobs", 4),
                    ("job-file", "/var/lib/runperf/runperf-nbd/nbd.fio"))
    base_path = "/var/lib/runperf/runperf-nbd/"

    def __init__(self, host, workers, base_output_path, metadata, extra):
        self.fio_job_file = extra.get("job-file", self.base_path + "nbd.fio")
        super().__init__(host, workers, base_output_path, metadata, extra)

    def setup(self):
        PBenchFio.setup(self)
        with open(os.path.join(os.path.dirname(__file__), "assets", "pbench",
                               "nbd-check.fio")) as fio_check:
            fio_check_tpl = utils.shell_write_content_cmd(self.base_path +
                                                          "nbd-check.fio",
                                                          fio_check.read())
        with open(os.path.join(os.path.dirname(__file__), "assets", "pbench",
                               "nbd.fio")) as fio:
            fio_tpl = utils.shell_write_content_cmd(self.fio_job_file,
                                                    fio.read())
        for workers in self.workers:
            for worker in workers:
                with worker.get_session_cont() as session:
                    session.cmd("mkdir -p " + self.base_path)
                    session.cmd(fio_check_tpl)
                    ret = session.cmd_status("fio --parse-only %s/"
                                             "nbd-check.fio" % self.base_path)
                    if ret:
                        raise exceptions.TestSkip("Fio %s does not support "
                                                  "ioengine=nbd on worker %s"
                                                  % (session.cmd("which fio"),
                                                     worker))
                    session.cmd("truncate -s 256M %s/disk.img" % self.base_path)
                    session.cmd("nohup qemu-nbd -t -k %s/socket"
                                " -f raw %s/disk.img &> "
                                "$(mktemp %s/qemu_nbd_XXXX.log)"
                                " & echo $! >> %s/kill_pids"
                                % ((self.base_path,) * 4))
                    # Sometimes nohup is not enough, use disown
                    session.cmd("for PID in $(cat %s/kill_pids); do "
                                "disown -h $PID; done" % self.base_path)
        with self.host.get_session_cont(hop=self.host) as session:
            session.cmd("mkdir -p " + self.base_path)
            session.cmd(fio_tpl)

    def cleanup(self):
        for workers in self.workers:
            for worker in workers:
                with worker.get_session_cont() as session:
                    pids = session.cmd("cat %s/kill_pids 2>/dev/null || true"
                                       % self.base_path)
                    for pid in pids.splitlines():
                        session.cmd_status("kill -9 '%s'" % pid)
                    session.cmd("rm -Rf " + self.base_path)
        with self.host.get_session_cont(hop=self.host) as session:
            session.cmd("rm -Rf %s" % self.base_path)
        PBenchFio.cleanup(self)

def get(name, extra):
    """
    Get list of test classes based on test name

    :param test_name: Test name optionally followed by ':' and extra params
    :return: instance that allow performing the test and extra params
    """
    return (utils.named_entry_point('runperf.tests', name), extra)
