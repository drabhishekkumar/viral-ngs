"""Recording of metadata"""

# built-ins
import os
import os.path
import stat
import sys
import argparse
import getpass
import socket
import collections
import traceback
import functools
import time
import uuid
import platform
import contextlib
import inspect

# intra-module
import util.file
import util.misc
import util.version
import util.cmd_plugins

from util._metadata.file_arg import FileArg
from util._metadata.hashing import Hasher
from util._metadata.md_utils import _shell_cmd, errors_as_warnings
from util._metadata import _log
from util._metadata import metadata_db

# third-party

VIRAL_NGS_METADATA_FORMAT='1.0.0'

@util.cmd_plugins.cmd_hookimpl
def cmd_handle_file_arg(val, mode, compute_fnames):
    """Handle a command-line argument denoting input or output file(s).
    Here we wrap the value in a FileArg object, to record, besides the original argument value, whether this arg denotes input or output
    files, and how to get the names of these files; in the recording module's implementation of the cmd_call_cmd() hook below,
    the FileArg objects will be replaced with the original argument values (`val`) before calling the original command.
    """
    return FileArg(val, mode, compute_fnames)

# ** run_id management

def create_run_id(t=None):
    """Generate a unique ID for a run (set of steps run as part of one workflow)."""
    return util.file.string_to_file_name('__'.join(map(str, (time.strftime('%Y%m%d%H%M%S', time.localtime(t))[2:], getpass.getuser(),
                                                             os.path.basename(os.getcwd()), uuid.uuid4()))))[:210]

def set_run_id():
    """Generate and record in the environment a unique ID for a run (set of steps run as part of one workflow)."""
    os.environ['VIRAL_NGS_METADATA_RUN_ID'] = create_run_id()

# ** Getting the execution environment

def save_dirty_repo(code_hash, tag, push_to):  # pragma: no cover
    """For use during development: if repo is dirty, create a tag for the current contents,
    and optionally push it to a specified repo.  Currently will not notice new files."""
    with util.file.pushd_popd(util.version.get_project_path()):
        stash_hash = _shell_cmd('git stash create', check=False, silent=True)
        if stash_hash:
            code_hash = stash_hash
            if tag:
                _shell_cmd('git tag ' + tag + ' ' + code_hash, check=False, silent=True)
                if push_to:
                    _shell_cmd('git push ' + push_to + ' ' + tagb, check=False, silent=True)
    return code_hash

def tag_code_version(tag, push_to=None):
    """Create a lightweight git tag for the current state of the project repository, even if the state is dirty.
    If the repository is dirty, use the 'git stash create' command to create a commit representing the current state,
    and tag that; else, tag the existing clean state.  If `push_to` is not None, push the tag to the specified git remote.
    Return the git hash for the created git tag.  In case of any error, print a warning and return an empty string.
    """

    code_hash = ''

    with errors_as_warnings('getting repo version'):
        git_dir = os.path.join(util.version.get_project_path(), '.git')
        git_head_fname = os.path.join(git_dir, 'HEAD')
        if os.path.isfile(git_head_fname):
            head_branch = util.file.slurp_file(git_head_fname).strip()
            if head_branch.startswith('ref:'):
                code_hash = util.file.slurp_file(os.path.join(git_dir, head_branch.split()[1])).strip()

        if 'VIRAL_NGS_METADATA_DETAILED_ENV' in os.environ:  # pragma: no cover
            code_hash = save_dirty_repo(code_hash, tag, push_to)
    return code_hash

def get_conda_env():
    """Return the active conda environment"""
    if 'VIRAL_NGS_METADATA_DETAILED_ENV' not in os.environ: return ''
    return _shell_cmd('conda env export')

def gather_version_info(step_id):
    """Gather info about the code version we are running"""
    # record the code version used to run this step
    code_repo = os.path.join(metadata_db.metadata_dir(), 'code_repo')
    code_hash = tag_code_version('cmd_' + step_id, push_to=code_repo if os.path.isdir(code_repo) else None)
    
    return dict(viral_ngs_version=util.version.get_version(),
                viral_ngs_path=util.version.get_project_path(),
                viral_ngs_path_real=os.path.realpath(util.version.get_project_path()),
                code_hash=code_hash)

def gather_run_env():
    """Gather runtime environment"""
    env_vars_to_save = (
        # if running on DNAnexus 
        'DX_JOB_ID', 'DX_PROJECT_CONTEXT_ID',
        # if running on travis-ci
        'TRAVIS_BUILD_ID', 'TRAVIS_JOB_ID', 'TRAVIS_COMMIT',
    )
    return dict(metadata_dir='',
                platform=platform.platform(), 
                cpus=util.misc.available_cpu_count(), host=socket.getfqdn(),
                user=getpass.getuser(),
                cwd=os.getcwd(), conda_env=get_conda_env(), 
                got_detailed_env='VIRAL_NGS_METADATA_DETAILED_ENV' in os.environ,
                env_vars = util.misc.dict_subset(os.environ, env_vars_to_save)
    )

def gather_run_info(beg_time, end_time, cmd_exception_str):
    return dict(beg_time=beg_time, end_time=end_time, duration=end_time-beg_time,
                exception=cmd_exception_str,
                argv=tuple(sys.argv))

def replace_file_args(args):
    # for args denoting input or output files, for which 'type=InFile' or 'type=OutFile' was used when adding the args to
    # the parser, the corresponding values will be of type FileArg, rather than strings.  We must convert these values
    # to str before calling the original command implementation `cmd_main`.
    def _replace_file_args(val):
        if isinstance(val, FileArg): return val.val
        if isinstance(val, (list, tuple)): return list(map(_replace_file_args, val))
        return val

    for arg, val in vars(args).items():
        setattr(args, arg, _replace_file_args(val))

def record_step_to_db(step_data, cmd_result):
    """Record the record of this step to the metadata database.  In the process, for any FileArg args of the command,
    gather hashsums and other file info for the denoted file(s)."""

    hasher = Hasher()

    def write_obj(x):
        """If `x` is a FileArg, return a dict representing it, else return a string representation of `x`.
        Used for json serialization below."""
        if not isinstance(x, FileArg): return str(x)
        file_info = x.gather_file_info(hasher, out_files_exist=not step_data['step']['run_info']['exception'],
                                       cmd_result=cmd_result)
        return file_info

    metadata_db.store_step_record(step_data=step_data, write_obj=write_obj)

# ** add_metadata_tracking

def add_metadata_arg(cmd_parser):
    """Add --metadata arg to `cmd_parser`"""
    if not getattr(cmd_parser, 'metadata_arg_added', False):
        cmd_parser.add_argument('--metadata', nargs=2, metavar=('ATTRIBUTE', 'VALUE'), action='append',
                                help='attach metadata to this step (step=this specific execution of this command)')
        setattr(cmd_parser, 'metadata_arg_added', True)

@util.cmd_plugins.cmd_hookimpl
def cmd_configure_parser(parser):
    add_metadata_arg(parser)

def gather_user_metadata(args_dict, cmd_result):
    # save any metadata specified on the command line.  then drop the 'metadata' argument from the args dict, since
    # the original command implementation `cmd_main` does not recognize this arg.
    metadata_from_cmd_line = { k[len('VIRAL_NGS_METADATA_VALUE_'):] : v
                               for k, v in os.environ.items() if k.startswith('VIRAL_NGS_METADATA_VALUE_') }
    metadata_from_cmd_line.update(dict(args_dict.pop('metadata', {}) or {}))

    # The function that implements the command can pass us some metadata to be included in the step record,
    # by returning a mapping with '__metadata__' as one key.  The value of that dict is a key that provides metadata.
    metadata_from_cmd_return = cmd_result.get('__metadata__', {}) if isinstance(cmd_result, collections.Mapping) else {}
    return metadata_from_cmd_line, metadata_from_cmd_return

@contextlib.contextmanager
def unpack_outcome(outcome):
    """Unpack command outcome, yielding (cmd_result, cmd_exception, cmd_exception_str).
    """
    cmd_result, cmd_exception, cmd_exception_str = None, None, None
    try:
        cmd_result = outcome.get_result()
        if cmd_result: cmd_result = cmd_result[0]
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        cmd_exception = e
        cmd_exception_str = traceback.format_exc()

    yield (cmd_result, cmd_exception, cmd_exception_str)

    if cmd_exception:
        raise cmd_exception

@util.cmd_plugins.cmd_hookimpl(hookwrapper=True, trylast=True)
def cmd_call_cmd(cmd_main, args, config):
    """Add provenance tracking to the given command.  

    Called from util.cmd.attach_main().
    
    Args:
        cmd_main: function implementing the command. Function takes one parameter, an argparse.Namespace, giving the values of the command's
             arguments.

    Returns:
        a wrapper for cmd_main, which has the same signature but adds metadata recording if enabled.
    """

    args_dict = vars(args).copy()

    step_data = dict(__viral_ngs_metadata__=True, format=VIRAL_NGS_METADATA_FORMAT)

    cmd_main_unwrapped = util.misc.unwrap(cmd_main)
    cmd_module=os.path.splitext(os.path.relpath(inspect.getsourcefile(cmd_main_unwrapped),
                                                util.version.get_project_path()))[0].replace(os.sep, '.')
    cmd_name = args_dict.get('command', cmd_main_unwrapped.__name__)

    # Determine the run id and the step id for this step.  A step is a particular invocation of a command; a run is a set
    # of steps invoked as part of one workflow, such as one Snakemake invocation.
    # run_id is the same for all steps run as part of a single workflow.
    # if not given in the environment, create a run_id for a one-step workflow consisting of just this step.
    beg_time = time.time()
    run_id = os.environ.get('VIRAL_NGS_METADATA_RUN_ID', create_run_id(beg_time))
    step_id = '__'.join(map(str, (create_run_id(beg_time), cmd_module, cmd_name)))

    with errors_as_warnings():
        if 'VIRAL_NGS_METADATA_SAVE_STEP_ID_TO' in os.environ:
            util.file.dump_file(os.environ['VIRAL_NGS_METADATA_SAVE_STEP_ID_TO'], step_id)
    
    replace_file_args(args)

    with util.misc.tmp_set_env('VIRAL_NGS_METADATA_STEPS_RUNNING', step_id, append=True, sep=':') as enclosing_steps:
        outcome = yield
        with unpack_outcome(outcome) as (cmd_result, cmd_exception, cmd_exception_str), errors_as_warnings():

            if metadata_db.is_metadata_tracking_enabled() and not enclosing_steps:

                end_time = time.time()

                _log.info('command {}.{} finished in {}s; exception={}'.format(cmd_module, cmd_name, end_time-beg_time, 
                                                                               cmd_exception_str))

                metadata_from_cmd_line, metadata_from_cmd_return = gather_user_metadata(args_dict, cmd_result)

                args_dict.pop('func_main', '')

                step_data['step'] = dict(step_id=step_id, run_id=run_id,
                                         cmd_module=cmd_module, cmd_name=cmd_name,
                                         version_info=gather_version_info(step_id),
                                         run_env=gather_run_env(),
                                         run_info=gather_run_info(beg_time, end_time, cmd_exception_str),
                                         args=args_dict,
                                         metadata_from_cmd_line=metadata_from_cmd_line,
                                         metadata_from_cmd_return=metadata_from_cmd_return,
                                         enclosing_steps=enclosing_steps)
                record_step_to_db(step_data, cmd_result)
                _log.info('metadata recording took {}s'.format(time.time() - end_time))
