import fnmatch
import logging
import os
import pathlib
import re
from io import StringIO

import runez
import yaml


LOG = logging.getLogger(__name__)


DEFAULT_CONFIG = """
ext: gz
always-clean:
  cpython:
    - __phello__.foo.py
    - __pycache__
    - idle_test/
    - test/
    - tests/

cpython-configure:
  - --enable-optimizations
  - --with-lto
  - --with-ensurepip=upgrade

cpython-symlink: bin/python bin/pip

windows:
  ext: zip

macos:
  allowed-system-libs: .*
  env:
    MACOSX_DEPLOYMENT_TARGET: 10.14
  cpython-modules: xz openssl gdbm
"""


class ConfigSource:
    """Settings from one config file"""

    def __init__(self, source, data):
        self.source = source
        self.data = data

    def __repr__(self):
        return "%s config" % runez.short(self.source)

    def represented(self):
        """Textual (yaml) representation of this config"""
        buffer = StringIO()
        yaml.dump(self.data, stream=buffer)
        return buffer.getvalue()

    def get_value(self, key):
        """
        Args:
            key (str | tuple): Key to look up, tuple represents hierarchy, ie: a/b -> (a, b)

        Returns:
            Associated value, if any
        """
        return self._deep_get(self.data, key)

    def _deep_get(self, data, key):
        if not key or not isinstance(data, dict):
            return None

        if isinstance(key, tuple):
            if len(key) > 1:
                value = self._deep_get(data, key[0])
                return self._deep_get(value, key[1:])

            key = key[0]

        value = data.get(key)
        if value is not None:
            return value


class Config:
    """Overall config, the 1st found (most specific) setting wins"""

    base_folder: pathlib.Path = None
    dist_folder: pathlib.Path = None
    main_build_folder: pathlib.Path = None
    path: pathlib.Path = None

    def __init__(self, path=None, base_folder=None, target=None, replaces=None):
        """
        Args:
            path (str): Path to config file
            base_folder (str | None): Base folder to use (for build/ and dist/ folders)
            target (str | runez.system.PlatformId | None): Target platform (for testing, defaults to current platform)
            replaces (Config): Internal: other config this config is replacing
        """
        if isinstance(replaces, Config):
            path = path or replaces.path
            base_folder = base_folder or replaces.base_folder

        elif base_folder:
            base_folder = runez.to_path(base_folder, no_spaces=True).absolute()

        if path:
            path = runez.to_path(path).absolute()

        if not isinstance(target, runez.system.PlatformId):
            target = runez.system.PlatformId(target)

        self.path = path
        self.target = target
        self.sources = []  # type: list[ConfigSource]
        self.by_path = {}
        if path:
            self.load(path)
            default = yaml.safe_load(DEFAULT_CONFIG)
            default = ConfigSource("default", default)
            self.sources.append(default)

        if base_folder:
            base_folder = runez.to_path(base_folder, no_spaces=True).absolute()

        if base_folder != self.base_folder:
            self.base_folder = base_folder
            self.main_build_folder = base_folder / "build"
            self.dist_folder = base_folder / "dist"

    def __repr__(self):
        return "%s, %s [%s]" % (runez.short(self.path), runez.plural(self.sources, "config source"), self.target)

    @property
    def use_github(self):
        return self.get_value("use-github")

    def get_value(self, *key):
        """
        Args:
            key (str | tuple): Key to look up, tuple represents hierarchy, ie: a/b -> (a, b)

        Returns:
            Associated value, if any
        """
        paths = self._key_paths(key)
        for k in paths:
            for source in self.sources:
                v = source.get_value(k)
                if v is not None:
                    return v

    def represented(self):
        """Textual (yaml) representation of all configs"""
        result = []
        for source in self.sources:
            result.append("%s:" % runez.bold(source))
            result.append(source.represented())

        return runez.joined(result, delimiter="\n")

    def cleanup_folder(self, module):
        folder = module.install_folder
        module_name = module.m_name
        version = module.version
        fmt = dict(mm=f"{version.major}.{version.minor}", version=version)
        always_cleaned = self.get_value("always-clean", module_name)
        module_specific = self.get_value("%s-clean" % module_name)
        cleanup_spec = runez.flattened(always_cleaned, module_specific, split=True)
        cleanup_spec = [x.format(**fmt) for x in cleanup_spec]
        if cleanup_spec:
            matcher = FileMatcher(cleanup_spec)
            LOG.info("Applying clean-up spec: %s" % matcher)
            cleaned = []
            for dirpath, dirnames, filenames in os.walk(folder):
                removed = []
                dirpath = runez.to_path(dirpath)
                for name in dirnames:
                    full_path = dirpath / name
                    if matcher.is_match(full_path):
                        removed.append(name)
                        cleaned.append(name)
                        runez.delete(full_path, logger=None)

                for name in removed:
                    dirnames.remove(name)

                for name in filenames:
                    full_path = dirpath / name
                    if matcher.is_match(full_path):
                        cleaned.append(name)
                        runez.delete(full_path, logger=None)

            if cleaned:
                names = runez.joined(sorted(set(cleaned)))
                LOG.info("Cleaned %s: %s" % (runez.plural(cleaned, "build artifact"), runez.short(names)))

    @staticmethod
    def real_path(path: pathlib.Path):
        if path and path.exists():
            if path.is_symlink():
                path = runez.to_path(os.path.realpath(path))

            return path

    @staticmethod
    def find_main_file(desired: pathlib.Path, version):
        p = Config.real_path(desired)
        if p:
            return p

        rp = desired.name
        candidates = (rp, "%s%s" % (rp, version.major), "%s%s.%s" % (rp, version.major, version.minor))
        for c in candidates:
            fc = Config.real_path(desired.parent / c)
            if fc:
                return fc

    def correct_symlinks(self, module):
        folder = module.install_folder
        version = module.version
        relative_paths = self.get_value("%s-symlink" % module.m_name)
        relative_paths = runez.flattened(relative_paths, split=True)
        if relative_paths:
            for rp in relative_paths:
                desired = folder / rp
                main_file = self.find_main_file(desired, version)
                if main_file and main_file != desired:
                    runez.symlink(main_file, desired, overwrite=False)
                    return

    @staticmethod
    def auto_correct_shebang(path: pathlib.Path, main_python: pathlib.Path):
        if path == main_python or not path.is_file() or path.is_symlink():
            return

        lines = []
        with open(path) as fh:
            try:
                for line in fh:
                    if lines:
                        lines.append(line)
                        continue

                    if not line.startswith("#!") or "bin/python" not in line:
                        return

                    lines.append("#!/bin/sh\n")
                    lines.append('"exec" "$(dirname $0)/%s" "$0" "$@"\n' % main_python.name)

            except UnicodeError:
                return

        if lines:
            LOG.info("Auto-corrected shebang for %s" % runez.short(path))
            with open(path, "wt") as fh:
                for line in lines:
                    fh.write(line)

    def load(self, path):
        if path.exists():
            with open(path) as fh:
                data = yaml.safe_load(fh)
                source = ConfigSource(path, data)
                self.sources.append(source)
                self.by_path[str(path)] = source
                include = source.get_value("include")
                if include:
                    include = runez.resolved_path(include, base=path.parent)
                    self.load(runez.to_path(include))

    def _key_paths(self, key):
        return (self.target.platform, self.target.arch, *key), (self.target.platform, *key), key


class FileMatcher:

    def __init__(self, clean_spec):
        self.matches = []
        for spec in clean_spec:
            self.matches.append(SingleFileMatch(spec))

    def __repr__(self):
        return runez.joined(self.matches)

    def is_match(self, path: pathlib.Path):
        for m in self.matches:
            if m.is_match(path):
                return path


class SingleFileMatch:

    _on_folder = False
    _rx_basename = None
    _rx_path = None

    def __init__(self, spec: str):
        self.spec = spec
        if spec.endswith("/"):
            spec = spec[:-1]
            self._on_folder = True

        if "/" in spec:
            # lib/*/config-{python_mm}-\w+/
            path = ".*/%s$" % os.path.dirname(spec).replace("*", ".*").strip("/")
            spec = os.path.basename(spec)
            self._rx_path = re.compile(path)

        self._rx_basename = spec

    def __repr__(self):
        return self.spec

    def is_match(self, path: pathlib.Path):
        if self._on_folder == path.is_dir():
            if self._rx_path:
                m = self._rx_path.match(str(path.parent))
                if not m:
                    return False

            return fnmatch.fnmatch(path.name, self._rx_basename)

        else:
            assert True
