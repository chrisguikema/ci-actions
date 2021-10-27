# Copyright 2021, Proofcraft Pty Ltd
#
# SPDX-License-Identifier: BSD-2-Clause

"""
Parse and represent build definitions.

The Build class represents build definitions.

Use `load_builds` to load a build list from a yaml file, `run_builds` to run
them, `run_build_script` for a standard test driver frame, and
`default_junit_results` for a standard place to leave a jUnit summary file.
"""

from junitparser.junitparser import Failure, Error
from platforms import ValidationException, Platform, platforms, load_yaml, mcs_unsupported

from typing import Optional, List
from junitparser import JUnitXml

import copy
import os
import shutil
import subprocess
import sys

# exported names:
__all__ = [
    "Build", "load_builds", "run_builds", "run_build_script", "junit_results", "sanitise_junit"
]

# where to expect jUnit results by default
junit_results = 'results.xml'

# colour codes
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31;1m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_WHITE = "\033[37m"
ANSI_BOLD = "\033[1m"


class Build:
    """Represents a build definition.

    Currently, this is mainly a name and a platform, with mode if not determined
    by platform, and optional build settings.

    See cparser-run/builds.yml for examples.
    """

    def __init__(self, entries: dict, default={}):
        """Construct a Build from yaml dictionary. Accept optional default build attributes."""
        self.mode = None
        self.app = None
        self.req = None
        self.success = None
        self.error = None
        self.settings = {}
        self.timeout = 600
        self.disabled = False
        self.image_base_name = "sel4test-driver"
        [self.name] = entries.keys()
        attribs = copy.deepcopy(default)
        # this potentially overwrites the default settings dict, we restore it later
        attribs.update(entries[self.name])
        self.__dict__.update(**attribs)

        if 'settings' in default:
            for k, v in default['settings'].items():
                if not k in self.settings:
                    self.settings[k] = v

        if self.get_mode():
            self.update_settings()

    def update_settings(self):
        p = self.get_platform()
        m = self.get_mode()
        if p.arch != "x86":
            self.settings[p.cmake_toolchain_setting(m)] = "TRUE"
        self.settings["PLATFORM"] = p.get_platform(m)
        # somewhat misnamed now; sets test output to parsable xml:
        self.settings["BAMBOO"] = "TRUE"
        self.files = p.image_names(m, self.image_base_name)
        if self.req == 'sim':
            self.settings["SIMULATION"] = "TRUE"

    def get_platform(self) -> Platform:
        """Return the Platform object for this build definition."""
        return platforms[self.platform]

    def get_mode(self) -> Optional[int]:
        """Return the mode (32/64) for this build; taken from platform if not defined"""
        if not self.mode and self.get_platform().get_mode():
            return self.get_platform().get_mode()
        else:
            return self.mode

    def settings_args(self):
        """Return the build settings as an argument list [-Dkey=val]"""
        return [f"-D{key}={val}" for (key, val) in self.settings.items()]

    def set_verification(self):
        """Make this a verification build"""
        self.settings["VERIFICATION"] = "TRUE"

    def is_verification(self) -> bool:
        return self.settings.get("VERIFICATION") != None

    def can_release(self):
        # Bamboo excludes RELEASE for RPI3:
        return not self.get_platform().name == 'RPI3'

    def set_release(self):
        if not self.can_release():
            raise ValidationException("not Build.can_release()")
        self.settings["RELEASE"] = "TRUE"

    def is_release(self) -> bool:
        return self.settings.get("RELEASE") != None

    def is_debug(self) -> bool:
        return not self.is_release() and not self.is_verification()

    def can_hyp(self) -> bool:
        return (
            self.get_platform().name == 'PC99' or
            self.get_mode() in self.get_platform().aarch_hyp
        )

    def set_hyp(self):
        if not self.can_hyp():
            raise ValidationException("not Build.can_hyp()")

        if self.get_mode() in self.get_platform().aarch_hyp:
            self.settings['ARM_HYP'] = "TRUE"
        elif self.get_platform().name == 'PC99':
            self.settings['KernelVTX'] = "TRUE"
        else:
            # should be unreachable because of self.can_hyp():
            raise ValidationException

    def is_hyp(self) -> bool:
        return self.settings.get("ARM_HYP") != None or self.settings.get("KernelVTX") != None

    def can_clang(self) -> bool:
        # clang 8 does not support riscv, and Bamboo has no clang for TX1 64:
        return not (
            self.get_platform().arch == 'riscv' or
            self.get_platform().name == 'TX1' and self.get_mode() == 64
        )

    def set_clang(self):
        if not self.can_clang():
            raise ValidationException("not Build.can_clang()")
        self.settings["TRIPLE"] = self.get_platform().get_triple(self.get_mode())

    def is_clang(self) -> bool:
        return self.settings.get("TRIPLE") != None

    def is_gcc(self) -> bool:
        return not self.is_clang()

    def can_mcs(self) -> bool:
        return not self.get_platform().name in mcs_unsupported

    def set_mcs(self):
        if not self.can_mcs():
            raise ValidationException("not Build.can_mcs()")
        self.settings['MCS'] = "TRUE"

    def is_mcs(self) -> bool:
        return self.settings.get('MCS') != None

    def can_smp(self) -> bool:
        return self.get_mode() in self.get_platform().smp

    def set_smp(self) -> bool:
        if not self.can_smp():
            raise ValidationException("not Build.can_smp()")
        self.settings['SMP'] = "TRUE"

    def is_smp(self) -> bool:
        return self.settings.get('SMP') != None

    def validate(self):
        if not self.get_mode():
            raise ValidationException("Build: no unique mode")
        if not self.get_platform():
            raise ValidationException("Build: no platform")
        if self.is_clang() and not self.can_clang():
            raise ValidationException("not Build.can_clang()")
        if self.is_mcs() and not self.can_mcs():
            raise ValidationException("not Build.can_mcs()")
        if self.is_smp() and not self.can_smp():
            raise ValidationException("not Build.can_smp()")
        if self.is_release() and not self.can_release():
            raise ValidationException("not Build.can_release()")
        if self.is_hyp() and not self.can_hyp():
            raise ValidationException("not Build.can_hyp()")

    def __repr__(self) -> str:
        return \
            f"Build('{self.name}': " '{' \
            f"'platform': {self.platform}, 'mode': {self.get_mode()}, " \
            f"'req': {self.req}, 'app': {self.app}, 'settings': {self.settings}, " \
            f"'success': {self.success}, 'base': {self.image_base_name}" '})'

    def is_disabled(self) -> bool:
        return self.disabled or self.get_platform().disabled

    def get_req(self) -> List[str]:
        req = self.req or self.get_platform().req
        if not req or req == []:
            return []
        if isinstance(req, str):
            return [req]
        else:
            return req

    def hw_run(self, log):
        if self.is_disabled():
            return [['echo', f"Platform {self.get_platform().name} disabled. Skipping."]], []

        machine = get_machine(self.get_req())
        if not machine:
            return [['echo', f"No machine for {self.name}."],
                    ['exit', '1']], []

        if machine.endswith("_pool"):
            return [['echo', f"Specify list of machines instead of pool for {self.name}."],
                    ['exit', '1']], []

        return [
            ['tar', 'xvzf', f"../{self.name}-images.tar.gz"],
            mq_print_lock(machine),
            mq_lock(machine),
            mq_run(self.success, machine, self.files,
                   completion_timeout=self.timeout,
                   lock_held=True,
                   key=job_key(),
                   log=log,
                   error_str=self.error)
        ], [mq_release(machine)]


def release_mq_locks(builds):
    """Release locks from this job; runs the commands instead of returning a list."""

    def run(command):
        """Print and run a command, ignoring failure."""
        try:
            print(" ".join(command))
            sys.stdout.flush()
            subprocess.run(command)
        except:
            pass

    # If builds are done by platform, there will be only one req in the end,
    # but we are not guaranteed that builds are always by platform.
    reqs = [b.get_req() for b in builds]
    req_set = []
    for req in reqs:
        if req and not req in req_set:
            req_set.append(req)

    for req in req_set:
        machine = get_machine(req)

        if machine:
            # cancel any processes still waiting for locks
            run(mq_cancel(machine))
            # release any locks we already have claimed
            run(mq_release(machine))
            # show lock status (should now show "free" or "locked for another job")
            run(mq_print_lock(machine))


def get_machine(req):
    if req == []:
        return None
    else:
        # assume jobs for the same platform are consecutive-ish in the build matrix
        job_index = int(os.environ.get('INPUT_INDEX', '$0')[1:])
        return req[job_index % len(req)]


def job_key():
    return os.environ.get('GITHUB_RUN_ID') + "-" + \
        os.environ.get('GITHUB_JOB') + "-" + \
        os.environ.get('INPUT_INDEX', '$0')[1:]


def mq_run(success_str: str,
           machine: str,
           files: List[str],
           retries: int = -1,
           lock_timeout: int = 8,
           completion_timeout: int = -1,
           log: Optional[str] = None,
           lock_held=False,
           keep_alive=False,
           key: Optional[str] = None,
           error_str: Optional[str] = None):
    """Machine queue mq.sh run command with arguments.

       Expects success marker, machine name, and boot image file(s).
       See output of mq.sh run for details on optional parameters."""

    command = ['time', 'mq.sh', 'run',
               '-c', success_str,
               '-s', machine,
               '-d', str(completion_timeout),
               '-t', str(retries),
               '-w', str(lock_timeout)]

    if log:
        command.extend(['-l', log])
    if lock_held:
        command.append('-n')
    if keep_alive:
        command.append('-a')
    if key:
        command.extend(['-k', key])
    if error_str:
        command.extend(['-e', error_str])
    for f in files:
        command.extend(['-f', f])

    return command


def mq_lock(machine: str) -> List[str]:
    """Get lock for a machine."""
    return ['time', 'mq.sh', 'sem', '-wait', machine, '-k', job_key()]


def mq_release(machine: str) -> List[str]:
    """Release lock on a machine."""
    return ['mq.sh', 'sem', '-signal', machine, '-k', job_key()]


def mq_cancel(machine: str) -> List[str]:
    """Cancel processes waiting on lock for a machine."""
    return ['mq.sh', 'sem', '-cancel', machine, '-k', job_key()]


def mq_print_lock(machine: str) -> List[str]:
    """Print lock status for machine."""
    return ['mq.sh', 'sem', '-info', machine]


def run(args: List[str]):
    """Echo + run command with arguments; raise exception on exit != 0"""

    printc(ANSI_YELLOW, "+++ " + " ".join(args))
    sys.stdout.flush()
    # Print output as it arrives. Some of the build commands take too long to
    # wait until all output is there. Keep stderr separate, but flush it.
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE,
                               stderr=sys.stderr, bufsize=1)
    for line in process.stdout:
        print(line.rstrip())
        sys.stdout.flush()
        sys.stderr.flush()
    ret = process.wait()
    if not ret == 0:
        raise subprocess.CalledProcessError(ret, args)


def printc(color: str, content: str):
    print(color + content + ANSI_RESET)


def summarise_junit(file_path: str) -> bool:
    """Parse jUnit output and show a summary.

    Returns True if there were no failures or errors, raises exception
    on IOError or XML parse errors."""

    xml = JUnitXml.fromfile(file_path)
    succeeded = xml.tests - (xml.failures + xml.errors + xml.skipped)
    success = xml.failures == 0 and xml.errors == 0

    col = ANSI_GREEN if success else ANSI_RED

    printc(col, "Test summary")
    printc(col, "------------")
    printc(ANSI_GREEN if success else "", f"succeeded: {succeeded}/{xml.tests}")
    if xml.skipped > 0:
        printc(ANSI_YELLOW, f"skipped:   {xml.skipped}")
    if xml.failures > 0:
        printc(ANSI_RED, f"failures:  {xml.failures}")
    if xml.errors > 0:
        printc(ANSI_RED, f"errors:    {xml.errors}")
    print()

    failures = {str(case.name) for case in xml
                if any([isinstance(r, Failure) or isinstance(r, Error) for r in case.result])}

    return success, list(failures)


# where junit results are left after sanitising:
parsed_junit_results = 'parsed_results.xml'

# default script segment to sanitise junit results for sel4test et al
# expects to run in build dir of a standard seL4 project setup
sanitise_junit = ["python3", "../projects/seL4_libs/libsel4test/tools/extract_results.py",
                  "-q", junit_results, parsed_junit_results]


def run_build_script(manifest_dir: str, name: str, script: List[str], final_script: List[str] = [],
                     junit: bool = False, junit_file: str = parsed_junit_results) -> bool:
    """Run a build script in a separate `build/` directory

    A build script is a list of commands, which itself is a list of command and
    arguments passed to subprocess.run().

    The build stops at the first failing step (or the end) and fails if any
    step fails.

    The steps in `final_script` are run after script, regardless of failures
    in `script`.
    """

    print(f"::group::{name}")
    printc(ANSI_BOLD, f"-----------[ start test {name} ]-----------")
    sys.stdout.flush()

    os.chdir(manifest_dir)

    build_dir = 'build'
    try:
        shutil.rmtree(build_dir)
    except IOError:
        pass

    os.mkdir(build_dir)
    os.chdir(build_dir)

    if junit:
        script = script + [sanitise_junit]

    success = True
    try:
        for line in script:
            run(line)
    except subprocess.CalledProcessError:
        printc(ANSI_RED, ">>> command failed, aborting.")
        success = False

    # alway run final script tasks, even in case of failure
    try:
        for line in final_script:
            run(line)
    except subprocess.CalledProcessError:
        success = False

    failures = []
    if success and junit:
        try:
            success, failures = summarise_junit(junit_file)
        except IOError:
            printc(ANSI_RED, f"Error reading {junit_file}")
            success = False
        except:
            printc(ANSI_RED, f"Error parsing {junit_file}")
            success = False

    printc(ANSI_BOLD, f"-----------[ end test {name} ]-----------")
    print("::endgroup::")
    # after group, so that it's easier to scan for failed jobs
    if success:
        printc(ANSI_GREEN, f"{name} succeeded")
    else:
        printc(ANSI_RED, f"{name} FAILED")
        if failures != []:
            max_print = 10
            printc(ANSI_RED, "Failed cases: " + ", ".join(failures[:max_print]) +
                   (" ..." if len(failures) > max_print else ""))
    print("")
    sys.stdout.flush()

    return success


def list_mult(xs: list, ys: list) -> list:
    """Cross product of two lists. The first list is expected to be a list of lists.
    Returns a list of lists.

    `list_mult([[a],[b]], [c,d]) == [[a,c], [a,d], [b,c], [b,d]]`
    """
    combinations = []
    for x in xs:
        for y in ys:
            combinations.append(x + [y])
    return combinations


def variants(var_dict: dict) -> list:
    """Generate the matrix (=list of lists) of all variants in a variant dict."""

    keys = list(var_dict.keys())
    if keys == []:
        return []

    all = [[(keys[0], v)] for v in var_dict[keys[0]]]
    for k in keys[1:]:
        all = list_mult(all, [(k, v) for v in var_dict[k]])

    return all


def variant_name(variant):
    """Return the naming postfix for a build variant."""
    return "_".join([str(v) for _, v in variant if v != ''])


def build_for_platform(platform, default={}):
    """Return a standard build for a given platform, apply optional defaults."""

    the_build = copy.deepcopy(default)
    the_build["platform"] = platform

    return Build({platform: the_build})


def build_for_variant(base_build: Build, variant, filter_fun=lambda x: True) -> Optional[Build]:
    """Make a build definition from a supplied base build and a build variant.

    Optionally takes a filter/validation function to reject specific build
    settings combinations."""

    if not base_build:
        return None

    var_dict = dict(variant)

    build = copy.deepcopy(base_build)
    build.name = build.name + "_" + variant_name(variant)

    mode = var_dict.get("mode") or build.get_mode()
    if mode in build.get_platform().modes:
        build.mode = mode
    else:
        return None

    # build.mode is now unique, more settings could apply
    build.update_settings()

    try:
        for feature, val in variant:
            if feature == 'mcs' and val != '':
                build.set_mcs()
            elif feature == 'smp' and val != '':
                build.set_smp()
            elif feature == 'hyp' and val != '':
                build.set_hyp()
            elif feature == 'debug' and val == 'release':
                build.set_release()
            elif feature == 'debug' and val == 'verification':
                build.set_verification()
            elif feature == 'debug' and val not in ['debug', 'verification', 'release']:
                print(f"Warning: ignoring unknown setting {feature}: {val}")
                raise ValidationException
            elif feature == 'compiler' and val == 'clang':
                build.set_clang()
            elif feature == 'app':
                build.app = val
            elif feature == 'req':
                build.req = val
                # might change simulation or other settings
                build.update_settings()
            else:
                pass
        build.validate()
    except ValidationException:
        return None

    return build if filter_fun(build) else None


def get_env_filters() -> list:
    """Process input env variables and return a build filter (list of dict)"""

    def get(var: str) -> Optional[str]:
        return os.environ.get('INPUT_' + var.upper())

    def to_list(string: str) -> list:
        return [s.strip() for s in string.split(',')]

    keys = ['march', 'arch', 'mode', 'compiler', 'debug', 'platform', 'name', 'app', 'req']
    filter = {k: to_list(get(k)) for k in keys if get(k)}
    # 'mode' expects integers:
    if 'mode' in filter:
        filter['mode'] = list(map(int, filter['mode']))
    return [filter]


def filtered(build: Build, build_filters: dict) -> Optional[Build]:
    """Return build if build matches filter criteria, otherwise None."""

    def match_dict(build: Build, f):
        """Return true if all criteria in the filter are true for this build."""
        for k, v in f.items():
            if k == 'arch':
                if not build.get_platform().arch in v:
                    return False
            elif k == 'march':
                if not build.get_platform().march in v:
                    return False
            elif k == 'platform':
                if not build.get_platform().name in [x.upper() for x in v]:
                    return False
            elif k == 'debug':
                if build.is_debug() and not 'debug' in v:
                    return False
                if build.is_release() and not 'release' in v:
                    return False
                if build.is_verification() and not 'verification' in v:
                    return False
            elif k == 'compiler':
                if build.is_clang():
                    if not 'clang' in v:
                        return False
                else:
                    if not 'gcc' in v:
                        return False
            elif k == 'mode':
                if build.get_mode() not in v:
                    return False
            elif k == 'mcs':
                if v != '' and not build.is_mcs():
                    return False
            elif k == 'smp':
                if v != '' and not build.is_smp():
                    return False
            elif k == 'hyp':
                if v != '' and not build.is_hyp():
                    return False
            elif k in ['name', 'app', 'req']:
                if not vars(build).get(k) in v:
                    return False
            elif not vars(build.get_platform()).get(k):
                return False
        return True

    if not build:
        return None

    if not build_filters or build_filters == []:
        return build

    for f in build_filters:
        if match_dict(build, f):
            return build

    return None


def load_builds(file_name: Optional[str], filter_fun=lambda x: True,
                yml: Optional[dict] = None) -> List[Build]:
    """Load a list of build definitions from yaml.

    Use provided yaml dict, or if None, load from file. One of file_name, yml
    must be not None.

    Applies defaults, variants, and build-filter from the yaml file.
    Takes an optional filtering function for removing unwanted builds."""

    yml = yml or load_yaml(file_name)

    default_build = yml.get("default", {})
    build_filters = yml.get("build-filter", [])
    env_filters = get_env_filters()
    all_variants = variants(yml.get("variants", {}))
    yml_builds = yml.get("builds", [])

    if yml_builds == []:
        base_builds = [build_for_platform(p, default_build) for p in platforms.keys()]
    else:
        base_builds = [Build(b, default_build) for b in yml_builds]

    if all_variants == []:
        builds = [b for b in base_builds
                  if b and filtered(b, build_filters) and filtered(b, env_filters)]
    else:
        builds = []
        for b in base_builds:
            for v in all_variants:
                build = build_for_variant(b, v, filter_fun)
                build = filtered(build, build_filters)
                build = filtered(build, env_filters)
                if build:
                    builds.append(build)

    return builds


def run_builds(builds: list, run_fun) -> int:
    """Run a list of build definitions, given a test driver function.

    Expects the current directory to be a manifest directory, in which
    tests are started (usually creating `build/` directory and running there).

    The driver function `run_fun` should take a directory (manifest dir)
    and a Build, and run this build, returning true iff the build was successful.
    """

    print()
    sys.stdout.flush()

    manifest_dir = os.getcwd()
    successes = []
    fails = []
    for build in builds:
        (successes if run_fun(manifest_dir, build) else fails).append(build.name)

    printc(ANSI_GREEN if fails == [] else "", "Successful tests: " + ", ".join(successes))
    if fails != []:
        print()
        printc(ANSI_RED, "FAILED tests: " + ", ".join(fails))

    return 0 if fails == [] else 1
