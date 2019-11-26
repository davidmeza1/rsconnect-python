
import contextlib
import json
import os
import random
import sys
import subprocess
import time
import traceback
from datetime import datetime
from os.path import basename, dirname, exists, join
from pprint import pformat

import click
from six.moves.urllib_parse import urlparse

from . import api
from .environment import EnvironmentException
from .bundle import make_html_bundle, make_source_bundle
from .metadata import ServerStore, AppStore

line_width = 45
verbose = False
server_store = ServerStore()
server_store.load()


@contextlib.contextmanager
def CLIFeedback(label):
    """Context manager for OK/ERROR feedback from the CLI.

    If the enclosed block succeeds, OK will be emitted.
    If it fails, ERROR will be emitted.
    Errors will also be classified as operational errors (prefixed with 'Error')
    vs. internal errors (prefixed with 'Internal Error'). In verbose mode,
    tracebacks will be emitted for internal errors.
    """
    if label:
        pad = line_width - len(label)
        click.secho(label + '... ' + ' ' * pad, nl=False, fg='bright_white')

    def passed():
        if label:
            click.secho('[', nl=False, fg='bright_white')
            click.secho('OK', fg='bright_green', nl=False)
            click.secho(']', fg='bright_white')

    def failed(err):
        if label:
            click.secho('[', nl=False, fg='bright_white')
            click.secho('ERROR', fg='red', nl=False)
            click.secho(']', fg='bright_white')
        click.secho(str(err), fg='bright_red')
        sys.exit(1)

    try:
        yield
        passed()
    except api.RSConnectException as exc:
        failed('Error: ' + exc.message)
    except EnvironmentException as exc:
        failed('Error: ' + str(exc))
    except Exception as exc:
        if verbose:
            traceback.print_exc()
        failed('Internal error: ' + str(exc))


def which_python(python, env=os.environ):
    """Determine which python binary should be used.

    In priority order:
    * --python specified on the command line
    * RETICULATE_PYTHON defined in the environment
    * the python binary running this script
    """
    if python:
        return python

    if 'RETICULATE_PYTHON' in env:
        return env['RETICULATE_PYTHON']

    return sys.executable


def inspect_environment(python, dir, check_output=subprocess.check_output):
    """Run the environment inspector using the specified python binary.

    Returns a dictionary of information about the environment,
    or containing an "error" field an an error occurred.
    """
    environment_json = check_output([python, '-m', 'rsconnect.environment', dir], universal_newlines=True)
    environment = json.loads(environment_json)
    return environment


def make_deployment_name():
    """Produce a unique name for this deployment as required by the Connect API.

    This is based on the current unix timestamp. Since the millisecond portion
    is zero on some systems, we add some jitter.
    """
    timestamp = int(1000 * time.mktime(datetime.now().timetuple())) + random.randint(0, 999)
    return 'deployment-%d' % timestamp


def default_title(filename):
    """Produce a default content title from the file path"""
    return basename(filename).rsplit('.')[0]


def output_task_log(task_status, last_status):
    """Echo any new output from the task to stdout.

    Returns an updated last_status which should be passed into
    the next call to output_task_log.

    Raises RSConnectException on task failure.
    """
    new_last_status = last_status
    if task_status['last_status'] != last_status:
        for line in task_status['status']:
            click.echo(line)
            new_last_status = task_status['last_status']

    if task_status['finished']:
        exit_code = task_status['code']
        if exit_code != 0:
            raise api.RSConnectException('Task exited with status %d.' % exit_code)

        click.secho('Deployment completed successfully.', fg='bright_white')
    return new_last_status


def do_ping(server, api_key, insecure, cacert):
    with CLIFeedback('Checking %s' % server):
        uri = urlparse(server)
        if not uri.netloc:
            raise api.RSConnectException('Invalid server URL: "%s"' % server)
        api.verify_server(server, insecure, cacert)
    
    if api_key:
        with CLIFeedback('Verifying API key'):
            uri = urlparse(server)
            api.verify_api_key(uri, api_key, insecure, cacert)


@click.group(no_args_is_help=True)
def cli():
    pass


@cli.command(help='Add a server')
@click.option('--name', '-n', required=True, help='Server nickname')
@click.option('--server', '-s', required=True, help='Connect server URL')
@click.option('--api-key','-k',required=True, help='Connect server API key')
@click.option('--insecure', is_flag=True, help='Disable TLS certification validation.')
@click.option('--cacert', type=click.File('rb'), help='Path to trusted TLS CA certificate.')
@click.option('--verbose', '-v', '_verbose', is_flag=True, help='Print detailed error messages on failure.')
def add(name, server, api_key, insecure, cacert, _verbose):
    global verbose
    verbose = _verbose

    old_server = server_store.get(name)

    # server must be pingable to be added
    do_ping(server, api_key, insecure, cacert)
    server_store.add(name, server, api_key, insecure, cacert)
    server_store.save()
    
    if old_server is None:
        click.echo('Added server "%s" with URL %s' % (name, server))
    else:
        click.echo('Replaced server "%s" with URL %s' % (name, server))


@cli.command(help='Remove a server')
@click.option('--verbose', '-v', '_verbose', is_flag=True, help='Print detailed error messages on failure.')
@click.argument('server')
def remove(server, _verbose):
    global verbose
    verbose = _verbose

    old_server = server_store.get(server)

    server_store.remove(server)
    server_store.save()
    
    if old_server is None:
        click.echo('Server "%s" was not found' % server)
    else:
        click.echo('Removed server "%s"' % server)


@cli.command('list', help='List saved servers')
@click.option('--verbose', '-v', '_verbose', is_flag=True, help='Print detailed error messages on failure.')
def list_servers( _verbose):
    global verbose
    verbose = _verbose

    servers = server_store.list()

    click.echo('Server information from %s' % server_store.get_path())

    if not servers:
        click.echo('No servers are saved. To save a server, see `rsconnect save --help`.')
    else:
        click.echo()
        for server in servers:
            click.echo('Server "%s"' % server['name'])
            click.echo('    URL: %s' % server['url'])
            if server['api_key']:
                click.echo('    API key is saved')
            if server['insecure']:
                click.echo('    Insecure mode (TLS certificate validation disabled)')
            if server['ca_cert']:
                click.echo('    TLS certificate file: %s' % server['ca_cert'])
            click.echo()


@cli.command(help='Show the version of rsconnect-python')
def version():
    version_file = join(dirname(__file__), 'version.txt')
    with open(version_file, 'r') as f:
        version = f.read().strip()
        click.echo(version)


@cli.command(help='Verify a Connect server URL')
@click.option('--server', '-s', required=True, envvar='CONNECT_SERVER', help='Connect server URL')
@click.option('--api-key','-k', envvar='CONNECT_API_KEY', help='Connect server API key')
@click.option('--insecure', envvar='CONNECT_INSECURE', is_flag=True, help='Disable TLS certification validation.')
@click.option('--cacert', envvar='CONNECT_CA_CERTIFICATE', type=click.File('rb'), help='Path to trusted TLS CA certificate.')
@click.option('--verbose', '-v', '_verbose', is_flag=True, help='Print detailed error messages on failure.')
def test(server, api_key, insecure, cacert, _verbose):
    global verbose
    verbose = _verbose

    do_ping(server, api_key, insecure, cacert)


@cli.command(help='Deploy content to RStudio Connect')
@click.option('--server', '-s', envvar='CONNECT_SERVER', help='Connect server URL')
@click.option('--api-key','-k', envvar='CONNECT_API_KEY', help='Connect server API key')
@click.option('--static', is_flag=True, help='Deployed a static, pre-rendered notebook. Static notebooks cannot be re-run on the server.')
@click.option('--new', '-n', is_flag=True, help='Force a new deployment, even if there is saved metadata from a previous deployment.')
@click.option('--app-id', help='Existing app ID or GUID to replace. Cannot be used with --new.')
@click.option('--title', '-t', help='Title of the content (default is the same as the filename)')
@click.option('--python', type=click.Path(exists=True), help='Path to python interpreter whose environment should be used. The python environment must have the rsconnect package installed.')
@click.option('--insecure', envvar='CONNECT_INSECURE', is_flag=True, help='Disable TLS certification validation.')
@click.option('--cacert', envvar='CONNECT_CA_CERTIFICATE', type=click.File('rb'), help='Path to trusted TLS CA certificate.')
@click.option('--verbose', '-v', '_verbose', is_flag=True, help='Print detailed error messages on failure.')
@click.argument('file', type=click.Path(exists=True))
@click.argument('extra_files', nargs=-1, type=click.Path())
def deploy(server, api_key, static, new, app_id, title, python, insecure, cacert, _verbose, file, extra_files):
    global verbose
    verbose = _verbose

    if server:
        click.secho('Deploying %s to server "%s"' % (file, server), fg='bright_white')
    else:
        click.secho('Deploying %s' % file, fg='bright_white')

    app_store = AppStore(file)
    app_store.load()

    with CLIFeedback('Checking arguments'):
        server, api_key, insecure, cacert = server_store.resolve(server, api_key, insecure, cacert)
        uri = urlparse(server)
        if not uri.netloc:
            raise api.RSConnectException('Invalid server URL: "%s"' % server)

        if not file.endswith('.ipynb'):
            raise api.RSConnectException('Only Jupyter notebooks (.ipynb files) can be deployed.')

        # we check the extra files ourselves, since they are paths relative to the base file
        for extra in extra_files:
            if not exists(join(dirname(file), extra)):
                raise api.RSConnectException('Could not find file %s in %s' % (extra, os.getcwd()))

        deployment_name = make_deployment_name()
        if not title:
            title = default_title(file)

        app_mode = 'static' if static else 'jupyter-static'

        if new:
            if app_id is not None:
                raise api.RSConnectException('Cannot specify both --new and --app-id.')
        else:
            # Redeployment. Use the saved app information unless overridden by the user.
            metadata = app_store.get(server)
            if metadata is not None:
                if verbose:
                    click.echo('Found previous deployment data in %s' % app_store.get_path())

                if app_id is None:
                    app_id = metadata.get('app_guid') or metadata.get('app_id')
                    if verbose:
                        click.echo('Using saved app ID: %s' % app_id)

                if title is None:
                    title = metadata.get('title')
                    if verbose:
                        click.echo('Using saved title: "%s"' % title)

                # app mode cannot be changed on redeployment
                app_mode = metadata.get('app_mode')
                if static and app_mode != 'static':
                    raise api.RSConnectException('Cannot change app mode to "static" once deployed. '
                                                 'Use --new to create a new deployment.')

            else:
                if verbose:
                    click.echo('No previous deployment to this server was found; this will be a new deployment.')

    with CLIFeedback('Inspecting python environment'):
        python = which_python(python)
        environment = inspect_environment(python, dirname(file))
        if verbose:
            click.echo('Python: %s' % python)
            click.echo('Environment: %s' % pformat(environment))

    with CLIFeedback('Creating deployment bundle'):
        if app_mode == 'static':
            bundle = make_html_bundle(file, title, python)
        else:
            bundle = make_source_bundle(file, environment, extra_files)

    with CLIFeedback('Uploading bundle'):
        app = api.deploy(uri, api_key, app_id, deployment_name, title, bundle, insecure, cacert)
        task_id = app['task_id']

    app_store.set(server, app['app_id'], None, title, app_mode)
    app_store.save()

    click.secho('\nDeployment log:', fg='bright_white')
    last_status = None

    while True:
        time.sleep(0.5)

        with CLIFeedback(''):
            task_status = api.task_get(uri, api_key, task_id, last_status, app['cookies'], insecure, cacert)
            last_status = output_task_log(task_status, last_status)

            if task_status['finished']:
                app_config = api.app_config(uri, api_key, app['app_id'], insecure, cacert)
                app_url = app_config['config_url']
                click.secho('App URL: %s' % app_url, fg='bright_white')
                break


cli()
click.echo()
