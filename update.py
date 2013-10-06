#!/usr/bin/env python

# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Red Hat, Inc.
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

# Taken from oslo commit 09baf99fc62 and modified for taskflow usage.

r"""
A simple script to update taskflows modules which have been copied
into other projects. See:

  http://wiki.openstack.org/CommonLibrary#Incubation

The script can be called the following ways:

  $> python update.py ../myproj
  $> python update.py --config-file ../myproj/taskflow.conf

Where ../myproj is a project directory containing taskflow.conf which
might look like:

  [DEFAULT]
  primitives = flow.linear_flow,flow.graph_flow,task
  base = myproj

Or:

  $> python update.py ../myproj/myconf.conf
  $> python update.py --config-file ../myproj/myconf.conf

Where ../myproj is a project directory which contains a differently named
configuration file, or:

  $> python update.py --config-file ../myproj/myproj/taskflow.conf
                      --dest-dir ../myproj

Where ../myproject is a project directory, but the configuration file is
stored in a sub-directory, or:

  $> python update.py --primitives flow.linear_flow --base myproj ../myproj
  $> python update.py --primitives flow.linear_flow,flow.graph_flow,task
                      --base myproj --dest-dir ../myproj

Where ../myproject is a project directory, but we explicitly specify
the primitives to copy and the base destination module

Obviously, the first way is the easiest!
"""

from __future__ import print_function

import ConfigParser

import collections
import functools
import logging
import os
import os.path
import re
import shutil
import sys
import textwrap

import six

LOG = logging.getLogger(__name__)

from oslo.config import cfg

BASE_MOD = 'taskflow'
OPTS = [
    cfg.ListOpt('primitives',
                default=[],
                help='The list of primitives to copy from %s' % BASE_MOD),
    cfg.StrOpt('base',
               default=None,
               help='The base module to hold the copy of %s' % BASE_MOD),
    cfg.StrOpt('dest-dir',
               default=None,
               help='Destination project directory'),
    cfg.StrOpt('configfile_or_destdir',
               default=None,
               help='A config file or destination project directory',
               positional=True),
    cfg.BoolOpt('verbose', default=False,
                short='v',
                help='Verbosely show what this program is doing'),
]
ALLOWED_PRIMITIVES = (
    'decorators',
    'engines',
    'exceptions',
    'flow',
    'persistence',
    'storage',
    'task',
)
IMPORT_FROM = re.compile(r"^\s*from\s+" + BASE_MOD + r"\s*(.*)$")
BASE_CONF = '%s.conf' % (BASE_MOD)
MACHINE_GENERATED = ('# DO NOT EDIT THIS FILE BY HAND -- YOUR CHANGES WILL BE '
                     'OVERWRITTEN', '')

ENTRY_FOOTER = [
    '',
    "Please make sure you have these installed.",
    '',
]
ENTRY_WARN = """
Please install stevedore [https://pypi.python.org/pypi/stevedore] to make
sure that entrypoints can be loaded successfully. A setup.cfg file which is
required for discovery of these entrypoints was %(updated_or_created)s at
'%(location)s' which requires either pbr [https://pypi.python.org/pypi/pbr/]
or distutils2 (which is provided by default in python 3.3+)
[https://pypi.python.org/pypi/Distutils2].
"""

# These module names require entrypoint adjustments to work correctly in the
# target projects namespace (they also require stevedore and a setup.cfg file
# that includes references to there module location).
REQUIRES_ENTRYPOINTS = {
    'engines.helpers': {
        'target_mod': 'engines.helpers',
        'replace': 'ENGINES_NAMESPACE',
        'replacement': '%s.taskflow.engines',
        'entrypoint': 'taskflow.engines',
    },
    'persistence.backends': {
        'target_mod': 'persistence.backends',
        'replace': 'BACKEND_NAMESPACE',
        'replacement': '%s.taskflow.persistence',
        'entrypoint': 'taskflow.persistence',
    },
}
REQUIRES_ENTRYPOINTS['engines'] = REQUIRES_ENTRYPOINTS['engines.helpers']


def _warn_entrypoint(cfg_file, there_existed):
    base_dir = os.path.basename(os.path.dirname(cfg_file))
    cfg_file = os.path.join(base_dir, os.path.basename(cfg_file))
    replacements = {
        'location': cfg_file,
    }
    if there_existed:
        replacements['updated_or_created'] = 'updated'
    else:
        replacements['updated_or_created'] = 'created'
    text = ENTRY_WARN.strip()
    text = text % replacements
    lines = ['']
    lines.extend(textwrap.wrap(text, width=79))
    lines.extend(ENTRY_FOOTER)
    for line in lines:
        LOG.warn(line)


def _configure_logging(cfg):
    if cfg.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


def _take_entrypoint_line(line, mod_list):
    line = line.strip()
    if not line or line.find("=") == -1 or line.startswith("#"):
        return True
    _name, module = line.split("=", 1)
    base_module = module.split(":")[0].strip()
    if base_module.startswith("%s." % (BASE_MOD)):
        base_module = _join_mod(*base_module.split(".")[1:])
    if not base_module:
        return False
    return _is_prefix_of(base_module, mod_list)


def _parse_args(argv):
    conf = cfg.ConfigOpts()
    conf.register_cli_opts(OPTS)
    conf(argv, usage='Usage: %(prog)s [config-file|dest-dir]')

    if conf.configfile_or_destdir:
        def def_config_file(dest_dir):
            return os.path.join(dest_dir, BASE_CONF)

        config_file = None
        if os.path.isfile(conf.configfile_or_destdir):
            config_file = conf.configfile_or_destdir
        elif (os.path.isdir(conf.configfile_or_destdir)
              and os.path.isfile(def_config_file(conf.configfile_or_destdir))):
            config_file = def_config_file(conf.configfile_or_destdir)

        if config_file:
            conf(argv + ['--config-file', config_file])

    return conf


def _explode_path(path):
    dirs = []
    dirs.append(path)
    (head, tail) = os.path.split(path)
    while tail:
        dirs.append(head)
        path = head
        (head, tail) = os.path.split(path)
    dirs.sort()
    return dirs


def _mod_to_path(mod):
    return os.path.join(*mod.split('.'))


def _dest_path(path, base, dest_dir):
    return os.path.join(dest_dir, _mod_to_path(base), path)


def _drop_init(path):
    with open(path, 'wb') as fh:
        for line in MACHINE_GENERATED:
            fh.write(line + '\n')


def _bulk_replace(path, pattern, replacement):
    with open(path, "rb+") as f:
        lines = f.readlines()
        f.seek(0)
        f.truncate()
        for line in lines:
            f.write(re.sub(pattern, replacement, line))


def _make_dirs(path):
    dir_name = os.path.dirname(path)
    dirs_needed = []
    for d in _explode_path(dir_name):
        if not os.path.isdir(d):
            dirs_needed.append(d)
    if dirs_needed:
        LOG.debug("Creating directories for '%s'", dir_name)
        for d in dirs_needed:
            LOG.debug(" '%s'", d)
            os.mkdir(d)
            init_path = os.path.join(d, '__init__.py')
            if not os.path.exists(init_path):
                LOG.debug(" '%s'", init_path)
                _drop_init(init_path)


def _join_mod(*pieces):
    return ".".join([str(p) for p in pieces if p])


def _reform_import(mod, postfix, alias, comment):
    assert mod, 'Module required'
    import_line = ''
    if mod and not postfix:
        import_line = 'import %s' % (mod)
    else:
        import_line = 'from %s import %s' % (mod, postfix)
    if alias:
        import_line += ' as %s' % (alias)
    if comment:
        import_line += '  #' + str(comment)
    return import_line


def _copy_file(path, dest, base, root_mods=None, common_already=None):

    def _copy_it():
        _make_dirs(dest)
        LOG.debug("Copying '%s'", path)
        LOG.debug(" '%s' -> '%s'", path, dest)
        shutil.copy2(path, dest)

    def _form_mod(prefix, postfix):
        importing = _join_mod(prefix, postfix)
        if importing not in common_already:
            new_mod = [base, BASE_MOD, prefix]
        else:
            new_mod = [base, 'openstack', 'common']
            # If the import is something like 'openstack.common.a.b.c.d'
            # ensure that we take the part after the first two
            # segments to ensure that we include it correctly.
            prefix_pieces = _split_mod(prefix)
            for p in prefix_pieces[2:]:
                new_mod.append(p)
        return _join_mod(*new_mod)

    def _import_replace(path):
        with open(path, "rb+") as f:
            lines = f.readlines()
            f.seek(0)
            f.truncate()
            new_lines = []
            for line in MACHINE_GENERATED:
                new_lines.append(line + "\n")
            new_lines.extend(lines)
            for (i, line) in enumerate(new_lines):
                segments = _parse_import_line(line, i + 1, path)
                if segments:
                    original_line = line
                    (comment, prefix, postfix, alias) = segments
                    line = "%s\n" % _reform_import(_form_mod(prefix, postfix),
                                                   postfix, alias, comment)
                    if original_line != line:
                        LOG.debug(" '%s' -> '%s'; line %s",
                                  original_line.strip(), line.strip(), i + 1)
                f.write(line)

    # Only bother making it if we already didn't make it...
    if not os.path.exists(dest):
        _copy_it()
        LOG.debug("Fixing up '%s'", dest)
        _import_replace(dest)
        _bulk_replace(dest,
                      'possible_topdir, "%s",$' % (BASE_MOD),
                      'possible_topdir, "' + base + '",')


def _get_mod_path(segments, base):
    if not segments:
        return (False, None)
    mod_path = _mod_to_path(_join_mod(base, *segments)) + '.py'
    if os.path.isfile(mod_path):
        return (True, mod_path)
    return (False, mod_path)


def _split_mod(text):
    pieces = text.split('.')
    return [p.strip() for p in pieces if p.strip()]


def _copy_pyfile(path, base, dest_dir, root_mods=None, common_already=None):
    _copy_file(path, _dest_path(path, base, dest_dir), base,
               common_already=common_already, root_mods=root_mods)


def _copy_mod(mod, base, dest_dir, common_already=None, root_mods=None):
    if not root_mods:
        root_mods = {}
    if not common_already:
        common_already = set()
    copy_pyfile = functools.partial(_copy_pyfile,
                                    base=base, dest_dir=dest_dir,
                                    common_already=common_already,
                                    root_mods=root_mods)
    # Ensure that the module has a root module if it has a mapping to one so
    # that its __init__.py file will exist.
    root_existed = False
    if mod in root_mods:
        root_existed = True
        copy_pyfile(root_mods[mod])
    exists, mod_file = _get_mod_path([mod], base=BASE_MOD)
    if exists:
        LOG.debug("Creating module '%s'", _join_mod(base, BASE_MOD, mod))
        copy_pyfile(mod_file)
        return mod_file
    else:
        if not root_existed:
            raise IOError("Can not find module: %s" % (_join_mod(BASE_MOD,
                                                                 mod)))
        return root_mods[mod]


def _parse_import_line(line, linenum=-1, filename=None):

    def blowup():
        msg = "Invalid import at '%s'" % (line)
        if linenum > 0:
            msg += "; line %s" % (linenum)
        if filename:
            msg += " from file '%s'" % (filename)
        raise IOError(msg)

    result = IMPORT_FROM.match(line)
    if not result:
        return None
    rest = result.group(1).split("#", 1)
    comment = ''
    if len(rest) > 1:
        comment = rest[1]
        rest = rest[0]
    else:
        rest = rest[0]
    if not rest:
        blowup()

    # Figure out the contents of a line like:
    #
    # from abc.xyz import blah as blah2

    # First looking at the '.xyz' part (if it exists)
    prefix = ''
    if rest.startswith("."):
        import_index = rest.find("import")
        if import_index == -1:
            blowup()
        before = rest[0:import_index - 1]
        before = before[1:]
        prefix += before
        rest = rest[import_index:]

    # Now examine the 'import blah' part.
    postfix = ''
    result = re.match(r"\s*import\s+(.*)$", rest)
    if not result:
        blowup()

    # Figure out if this is being aliased and keep the alias.
    importing = result.group(1).strip()
    result = re.match(r"(.*?)\s+as\s+(.*)$", importing)
    alias = ''
    if not result:
        postfix = importing
    else:
        alias = result.group(2).strip()
        postfix = result.group(1).strip()
    return (comment, prefix, postfix, alias)


def _find_import_modules(srcfile, root_mods):
    with open(srcfile, 'rb') as f:
        lines = f.readlines()
    for (i, line) in enumerate(lines):
        segments = _parse_import_line(line, i + 1, srcfile)
        if not segments:
            continue
        (comment, prefix, postfix, alias) = segments
        importing = _join_mod(prefix, postfix)
        if importing in root_mods.keys():
            yield importing
            continue
        # Attempt to locate where the module is by popping import
        # segments until we find one that actually exists.
        import_segments = _split_mod(importing)
        while len(import_segments):
            exists, _mod_path = _get_mod_path(import_segments, base=BASE_MOD)
            if exists:
                break
            else:
                import_segments.pop()
        prefix_segments = _split_mod(prefix)
        if not import_segments or len(import_segments) < len(prefix_segments):
            raise IOError("Unable to find import '%s'; line %s from file"
                          " '%s'" % (importing, i + 1, srcfile))
        yield _join_mod(*import_segments)


def _build_dependency_tree():
    dep_tree = {}
    root_mods = {}
    file_paths = []
    for dirpath, _tmp, filenames in os.walk(BASE_MOD):
        for filename in [x for x in filenames if x.endswith('.py')]:
            if dirpath == BASE_MOD:
                mod_name = filename.split('.')[0]
                root_mods[mod_name] = os.path.join(dirpath, '__init__.py')
            else:
                mod_pieces = dirpath.split(os.sep)[1:]
                mod_pieces.append(filename.split('.')[0])
                mod_name = _join_mod(*mod_pieces)
            if mod_name.endswith('__init__') or filename == '__init__.py':
                segments = _split_mod(mod_name)[0:-1]
                if segments:
                    mod_name = _join_mod(*segments)
                    root_mods[mod_name] = os.path.join(dirpath, filename)
            filepath = os.path.join(dirpath, filename)
            if mod_name:
                file_paths.append((filepath, mod_name))
    # Analyze the individual files dependencies after we know exactly what the
    # modules are so that we can find those modules if a individual file
    # imports a module instead of a file.
    for filepath, mod_name in file_paths:
        dep_list = dep_tree.setdefault(mod_name, [])
        dep_list.extend([x for x in _find_import_modules(filepath, root_mods)
                         if x != mod_name and x not in dep_list])
    return (dep_tree, root_mods)


def _dfs_dependency_tree(dep_tree, mod_name, mod_list=[]):
    mod_list.append(mod_name)
    for mod in dep_tree.get(mod_name, []):
        if mod not in mod_list:
            mod_list = _dfs_dependency_tree(dep_tree, mod, mod_list)
    return mod_list


def _complete_engines(engine_types):
    if not engine_types:
        return []
    engine_mods = [
        'engines',
        'engines.base',
    ]
    for engine_type in engine_types:
        engine_type = engine_type.strip()
        if not engine_type or engine_type in engine_mods:
            continue
        engine_mods.append(_join_mod('engines', engine_type))
        mod = _join_mod('engines', engine_type, 'engine')
        exists, mod_path = _get_mod_path([mod], base=BASE_MOD)
        if not exists:
            raise IOError("Engine %s file not found at: %s" % (engine_type,
                                                               mod_path))
        engine_mods.append(mod)
    return engine_mods


def _complete_flows(patterns):
    if not patterns:
        return []
    pattern_mods = [
        'patterns',
    ]
    for p in patterns:
        p = p.strip()
        if not p or p in pattern_mods:
            continue
        mod = _join_mod('patterns', p)
        exists, mod_path = _get_mod_path([mod], base=BASE_MOD)
        if not exists:
            raise IOError("Flow pattern %s file not found at: %s"
                          % (p, mod_path))
        pattern_mods.append(mod)
    return pattern_mods


def _complete_persistence(backends):
    if not backends:
        return []
    backend_mods = [
        'persistence',
        'persistence.logbook',
    ]
    for b in backends:
        b = b.strip()
        if not b or b in backend_mods:
            continue
        mod = _join_mod("persistence", "backends", b)
        exists, mod_path = _get_mod_path([mod], base=BASE_MOD)
        if not exists:
            raise IOError("Persistence backend %s file not found at: %s"
                          % (b, mod_path))
        backend_mods.append(mod)
    return backend_mods


def _is_prefix_of(prefix_text, haystack):
    for t in haystack:
        if t.startswith(prefix_text):
            return True
    return False


def _complete_module_list(base):
    dep_tree, root_mods = _build_dependency_tree()
    mod_list = []
    for mod in base:
        for x in _dfs_dependency_tree(dep_tree, mod, []):
            if x not in mod_list and x not in base:
                mod_list.append(x)
    mod_list.extend(base)
    # Ensure that we connect the roots of the mods to the mods themselves
    # and include them in the list of mods to be completed so they are included
    # also.
    for m in root_mods.keys():
        if _is_prefix_of(m, base) and m not in mod_list:
            mod_list.append(m)
    return (mod_list, root_mods)


def _find_existing(mod, base, dest_dir):
    mod = _join_mod(base, mod)
    mod_path = os.path.join(dest_dir, _mod_to_path(mod)) + '.py'
    if os.path.isfile(mod_path):
        return mod
    return None


def _uniq_itr(itr):
    seen = set()
    for i in itr:
        if i in seen:
            continue
        seen.add(i)
        yield i


def _rm_tree(base):
    dirpaths = []
    for dirpath, _tmp, filenames in os.walk(base):
        LOG.debug(" '%s' (X)", dirpath)
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            LOG.debug(" '%s' (X)", filepath)
            os.unlink(filepath)
        dirpaths.append(dirpath)
    for d in reversed(dirpaths):
        shutil.rmtree(d)


def main(argv):
    conf = _parse_args(argv)
    script_base = os.path.abspath(os.path.dirname(__file__))
    _configure_logging(conf)

    dest_dir = conf.dest_dir
    if not dest_dir and conf.config_file:
        dest_dir = os.path.dirname(os.path.abspath(conf.config_file[-1]))

    if not dest_dir or not os.path.isdir(dest_dir):
        print("A valid destination dir is required", file=sys.stderr)
        sys.exit(1)

    primitives = [p for p in _uniq_itr(conf.primitives)]
    primitive_types = collections.defaultdict(list)
    for p in primitives:
        try:
            p_type, p = p.split(".", 1)
        except ValueError:
            p_type = p
            p = ''
        p_type = p_type.strip()
        p = p.strip()
        if not p_type:
            continue
        if p not in primitive_types[p_type]:
            primitive_types[p_type].append(p)

    # TODO(harlowja): for now these are the only primitives we are allowing to
    # be copied over. Later add more as needed.
    unknown_prims = []
    for k in primitive_types.keys():
        if k not in ALLOWED_PRIMITIVES:
            unknown_prims.append(k)
    if unknown_prims:
        allowed = ", ".join(sorted(ALLOWED_PRIMITIVES))
        unknown = ", ".join(sorted(unknown_prims))
        print("Unknown primitives (%s) are being copied "
              "(%s is allowed)" % (unknown, allowed), file=sys.stderr)
        sys.exit(1)

    if not conf.base:
        print("A destination base module is required", file=sys.stderr)
        sys.exit(1)

    base_dir = os.path.join(dest_dir, conf.base)

    def copy_mods(mod_list, root_mods):
        common_already = {}
        missing_common = set()
        # Take out the openstack.common modules that exist already in the
        # containing project.
        mod_list = list(mod_list)
        for mod in list(mod_list):
            # NOTE(harlowja): attempt to use the modules being copied to common
            # folder as much as possible for modules that are needed for
            # taskflow as this avoids duplicating openstack.common in the
            # contained project as well as in the taskflow subfolder.
            if mod.startswith("openstack.common"):
                existing_mod = _find_existing(mod, conf.base, dest_dir)
                if existing_mod:
                    common_already[mod] = existing_mod
                    mod_list.remove(mod)
                else:
                    missing_common.add(mod)
        LOG.info("Copying %s modules into '%s'", len(mod_list), base_dir)
        for m in mod_list:
            LOG.info("  - %s", m)
        if common_already:
            LOG.info("The following modules will be used from the containing"
                     " projects 'openstack.common'")
            for mod in sorted(common_already.keys()):
                target_mod = common_already[mod]
                LOG.info("  '%s' -> '%s'", mod, target_mod)
        if missing_common:
            LOG.info("The following modules will *not* be used from the"
                     " containing projects 'openstack.common'")
            for mod in sorted(missing_common):
                LOG.info("  - %s", mod)
        copied = set()
        for mod in mod_list:
            copied.add(_copy_mod(mod, conf.base, dest_dir,
                                 common_already=common_already,
                                 root_mods=root_mods))
        LOG.debug("Copied %s modules", len(copied))
        for m in sorted(copied):
            LOG.debug("  - %s", m)

    def clean_old():
        old_base = os.path.join(dest_dir, conf.base, BASE_MOD)
        if os.path.isdir(old_base):
            LOG.info("Removing old %s tree found at '%s'", BASE_MOD, old_base)
            _rm_tree(old_base)

    def create_entrypoints(mod_list, root_mods):
        needed_entrypoints = set()
        for k in REQUIRES_ENTRYPOINTS.keys():
            for m in mod_list:
                if m.startswith(k):
                    needed_entrypoints.add(k)
        if not needed_entrypoints:
            return
        # Alter the source code locations that have the entry point name.
        LOG.info("Altering %s entrypoint referencing modules:",
                 len(needed_entrypoints))
        for m in sorted(needed_entrypoints):
            LOG.info("  - %s", m)
        entrypoints_adjusted = set()
        for k in sorted(needed_entrypoints):
            entrypoint_details = REQUIRES_ENTRYPOINTS[k]
            entrypoint_target = entrypoint_details['target_mod']
            there_entrypoint = (entrypoint_details['replacement'] % conf.base)
            if entrypoint_target in entrypoints_adjusted:
                continue
            base_mod_path = root_mods.get(entrypoint_target)
            if not base_mod_path:
                existing_mod = _find_existing(entrypoint_target,
                                              BASE_MOD, base_dir)
                if existing_mod:
                    base_mod_path = _mod_to_path(existing_mod) + ".py"
            if not base_mod_path:
                raise IOError("Could not find entrypoint target %s" %
                              entrypoint_target)
            dest_path = os.path.join(base_dir, base_mod_path)
            if not os.path.isfile(dest_path):
                raise IOError("Could not find entrypoint file %s" %
                              dest_path)
            LOG.debug("Adjusting '%s' in '%s'", entrypoint_details['replace'],
                      dest_path)
            pattern = r"%s\s*=.*" % (entrypoint_details['replace'])
            replacement = entrypoint_details['replace']
            replacement += " = '%s'" % (there_entrypoint)
            LOG.debug("Replacing '%s' -> '%s'", pattern, replacement)
            _bulk_replace(dest_path, pattern, replacement)
            entrypoints_adjusted.add(entrypoint_target)
        if not entrypoints_adjusted:
            return
        # Adjust there entrypoint configuration file (if it exists).
        cfg_filename = os.path.join(dest_dir, "setup.cfg")
        my_cfg_filename = os.path.join(script_base, 'setup.cfg')
        LOG.debug("Adjusting entrypoint configuration in '%s' with entrypoints"
                  " from '%s'", cfg_filename, my_cfg_filename)
        # Clear out there old entry points for taskflow
        there_cfg = ConfigParser.RawConfigParser()
        there_cfg.read([cfg_filename])
        there_exists = os.path.isfile(cfg_filename)
        for k in entrypoints_adjusted:
            entrypoint_details = REQUIRES_ENTRYPOINTS[k]
            entrypoint = entrypoint_details['entrypoint']
            there_entrypoint = (entrypoint_details['replacement'] % conf.base)
            try:
                there_cfg.remove_option('entry_points', there_entrypoint)
            except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
                pass
        # Copy and modify my entry points into there entrypoints.
        my_cfg = ConfigParser.RawConfigParser()
        my_cfg.read([my_cfg_filename])
        for k in sorted(entrypoints_adjusted):
            entrypoint_details = REQUIRES_ENTRYPOINTS[k]
            entrypoint = entrypoint_details['entrypoint']
            there_entrypoint = (entrypoint_details['replacement'] % conf.base)
            my_entries = my_cfg.get('entry_points', entrypoint)
            there_entries = []
            for line in my_entries.splitlines():
                # NOTE(harlowja): only take the entrypoints that are relevant
                # for the desired module list, skip the ones that are not.
                if _take_entrypoint_line(line, mod_list):
                    new_line = re.sub(entrypoint, there_entrypoint, line)
                    there_entries.append(new_line)
            try:
                there_cfg.add_section('entry_points')
            except ConfigParser.DuplicateSectionError:
                pass
            entry_value = os.linesep.join(there_entries)
            there_cfg.set('entry_points', there_entrypoint, entry_value)
            LOG.debug("Added entrypoint '%s'", there_entrypoint)
            for line in there_entries:
                line = line.strip()
                if line:
                    LOG.debug(">> %s", line)
        # ConfigParser seems to use tabs, instead of spaces, why!
        buf = six.StringIO()
        there_cfg.write(buf)
        contents = buf.getvalue()
        if contents.find("\t") != -1:
            contents = contents.replace("\t", " " * 4)
        if contents.find(" \n") != -1:
            contents = contents.replace(' \n', '\n')
        with open(cfg_filename, "wb") as fh:
            fh.write(contents)
        _warn_entrypoint(cfg_filename, there_exists)

    find_what = _complete_flows(primitive_types.pop('flow', []))
    find_what.extend(_complete_engines(primitive_types.get('engines')))
    find_what.extend(_complete_persistence(primitive_types.get('persistence')))
    find_what.extend(primitive_types.keys())
    find_what = [f for f in _uniq_itr(find_what)]
    copy_what, root_mods = _complete_module_list(find_what)
    copy_what = sorted([m for m in _uniq_itr(copy_what)])
    if copy_what:
        clean_old()
        copy_mods(copy_what, root_mods)
        create_entrypoints(copy_what, root_mods)
    else:
        print("Nothing to copy.", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
