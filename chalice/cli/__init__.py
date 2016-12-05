"""Command line interface for chalice.

Contains commands for deploying chalice.

"""
import os
import json
import sys
import logging
import zipfile
import tempfile
import importlib
import shutil

import click
import botocore.exceptions
import botocore.session
from typing import Dict, Any  # noqa

from lib.chalice.app import Chalice  # noqa
from lib.chalice import deployer
from lib.chalice import __version__ as chalice_version
from lib.chalice.logs import LogRetriever
from lib.chalice import prompts
from lib.chalice.config import Config
from lib.chalice.awsclient import TypedAWSClient


TEMPLATE_APP = """\
from chalice import Chalice

app = Chalice(app_name='%s')


@app.route('/')
def index():
    return {'hello': 'world'}


# The view function above will return {"hello": "world"}
# whenver you make an HTTP GET request to '/'.
#
# Here are a few more examples:
#
# @app.route('/hello/{name}')
# def hello_name(name):
#    # '/hello/james' -> {"hello": "james"}
#    return {'hello': name}
#
# @app.route('/users', methods=['POST'])
# def create_user():
#     # This is the JSON body the user sent in their POST request.
#     user_as_json = app.json_body
#     # Suppose we had some 'db' object that we used to
#     # read/write from our database.
#     # user_id = db.create_user(user_as_json)
#     return {'user_id': user_id}
#
# See the README documentation for more examples.
#
"""
GITIGNORE = """\
.chalice/deployments/
.chalice/venv/
"""


def create_botocore_session(profile=None, debug=False):
    # type: (str, bool) -> botocore.session.Session
    session = botocore.session.Session(profile=profile)
    session.user_agent_extra = 'chalice/%s' % chalice_version
    if debug:
        session.set_debug_logger('')
        inject_large_request_body_filter()
    return session


def show_lambda_logs(config, max_entries, include_lambda_messages):
    # type: (Config, int, bool) -> None
    lambda_arn = config.lambda_arn
    profile = config.profile
    client = create_botocore_session(profile).create_client('logs')
    retriever = LogRetriever.create_from_arn(client, lambda_arn)
    events = retriever.retrieve_logs(
        include_lambda_messages=include_lambda_messages,
        max_entries=max_entries)
    for event in events:
        print event['timestamp'], event['logShortId'], event['message'].strip()


def load_project_config(project_dir):
    # type: (str) -> Dict[str, Any]
    """Load the chalice config file from the project directory.

    :raise: OSError/IOError if unable to load the config file.

    """
    config_file = os.path.join(project_dir, '.chalice', 'config.json')
    with open(config_file) as f:
        return json.loads(f.read())


def load_chalice_app(project_dir):
    # type: (str) -> Chalice
    if project_dir not in sys.path:
        sys.path.append(project_dir)
    try:
        app = importlib.import_module('app')
        chalice_app = getattr(app, 'app')
    except Exception as e:
        exception = click.ClickException(
            "Unable to import your app.py file: %s" % e
        )
        exception.exit_code = 2
        raise exception
    return chalice_app


def inject_large_request_body_filter():
    # type: () -> None
    log = logging.getLogger('botocore.endpoint')
    log.addFilter(LargeRequestBodyFilter())


def create_config_obj(ctx, stage_name=None, autogen_policy=None, profile=None):
    # type: (click.Context, str, bool, str) -> Config
    user_provided_params = {}  # type: Dict[str, Any]
    project_dir = ctx.obj['project_dir']
    default_params = {'project_dir': project_dir}
    try:
        config_from_disk = load_project_config(project_dir)
    except (OSError, IOError):
        click.echo("Unable to load the project config file. "
                   "Are you sure this is a chalice project?")
        raise click.Abort()
    app_obj = load_chalice_app(project_dir)
    user_provided_params['chalice_app'] = app_obj
    if stage_name is not None:
        user_provided_params['stage'] = stage_name
    if autogen_policy is not None:
        user_provided_params['autogen_policy'] = autogen_policy
    if profile is not None:
        user_provided_params['profile'] = profile
    config = Config(user_provided_params, config_from_disk, default_params)
    return config


class LargeRequestBodyFilter(logging.Filter):
    def filter(self, record):
        # type: (Any) -> bool
        # Note: the proper type should be "logging.LogRecord", but
        # the typechecker complains about 'Invalid index type "int" for "dict"'
        # so we're using Any for now.
        if record.msg.startswith('Making request'):
            if record.args[0].name in ['UpdateFunctionCode', 'CreateFunction']:
                # When using the ZipFile argument (which is used in chalice),
                # the entire deployment package zip is sent as a base64 encoded
                # string.  We don't want this to clutter the debug logs
                # so we don't log the request body for lambda operations
                # that have the ZipFile arg.
                record.args = (record.args[:-1] +
                               ('(... omitted from logs due to size ...)',))
        return True


@click.group()
@click.version_option(version=chalice_version, message='%(prog)s %(version)s')
@click.option('--project-dir',
              help='The project directory.  Defaults to CWD')
@click.option('--debug/--no-debug',
              default=False,
              help='Print debug logs to stderr.')
@click.pass_context
def cli(ctx, project_dir, debug=False):
    # type: (click.Context, str, bool) -> None
    if project_dir is None:
        project_dir = os.getcwd()
    ctx.obj['project_dir'] = project_dir
    ctx.obj['debug'] = debug
    os.chdir(project_dir)


@cli.command()
@click.pass_context
def local(ctx):
    # type: (click.Context) -> None
    app_obj = load_chalice_app(ctx.obj['project_dir'])
    run_local_server(app_obj)


@cli.command()
@click.option('--autogen-policy/--no-autogen-policy',
              default=True,
              help='Automatically generate IAM policy for app code.')
@click.option('--profile', help='Override profile at deploy time.')
@click.argument('stage', nargs=1, required=False)
@click.pass_context
def deploy(ctx, autogen_policy, profile, stage):
    # type: (click.Context, bool, str, str) -> None
    config = create_config_obj(
        ctx, stage_name=stage, autogen_policy=autogen_policy,
        profile=profile)
    session = create_botocore_session(profile=config.profile,
                                      debug=ctx.obj['debug'])
    d = deployer.create_default_deployer(session=session, prompter=click)
    try:
        d.deploy(config)
    except botocore.exceptions.NoRegionError:
        e = click.ClickException("No region configured. "
                                 "Either export the AWS_DEFAULT_REGION "
                                 "environment variable or set the "
                                 "region value in our ~/.aws/config file.")
        e.exit_code = 2
        raise e


@cli.command()
@click.option('--num-entries', default=None, type=int,
              help='Max number of log entries to show.')
@click.option('--include-lambda-messages/--no-include-lambda-messages',
              default=False,
              help='Controls whether or not lambda log messages are included.')
@click.pass_context
def logs(ctx, num_entries, include_lambda_messages):
    # type: (click.Context, int, bool) -> None
    config = create_config_obj(ctx)
    show_lambda_logs(config, num_entries, include_lambda_messages)


@cli.command('gen-policy')
@click.option('--filename',
              help='The filename to analyze.  Otherwise app.py is assumed.')
@click.pass_context
def gen_policy(ctx, filename):
    # type: (click.Context, str) -> None
    from chalice import policy
    if filename is None:
        filename = os.path.join(ctx.obj['project_dir'], 'app.py')
    if not os.path.isfile(filename):
        click.echo("App file does not exist: %s" % filename)
        raise click.Abort()
    with open(filename) as f:
        contents = f.read()
        generated = policy.policy_from_source_code(contents)
        click.echo(json.dumps(generated, indent=2))


@cli.command('new-project')
@click.argument('project_name', required=False)
@click.option('--profile', required=False)
def new_project(project_name, profile):
    # type: (str, str) -> None
    if project_name is None:
        project_name = prompts.getting_started_prompt(click)
    if os.path.isdir(project_name):
        click.echo("Directory already exists: %s" % project_name)
        raise click.Abort()
    chalice_dir = os.path.join(project_name, '.chalice')
    os.makedirs(chalice_dir)
    config = os.path.join(project_name, '.chalice', 'config.json')
    cfg = {
        'app_name': project_name,
        'stage': 'dev'
    }
    if profile:
        cfg['profile'] = profile
    with open(config, 'w') as f:
        f.write(json.dumps(cfg, indent=2))
    with open(os.path.join(project_name, 'requirements.txt'), 'w'):
        pass
    with open(os.path.join(project_name, 'app.py'), 'w') as f:
        f.write(TEMPLATE_APP % project_name)
    with open(os.path.join(project_name, '.gitignore'), 'w') as f:
        f.write(GITIGNORE)


@cli.command('url')
@click.pass_context
def url(ctx):
    # type: (click.Context) -> None
    config = create_config_obj(ctx)
    session = create_botocore_session(profile=config.profile,
                                      debug=ctx.obj['debug'])
    c = TypedAWSClient(session)
    rest_api_id = c.get_rest_api_id(config.app_name)
    stage_name = config.stage
    region_name = c.region_name
    click.echo(
        "https://{api_id}.execute-api.{region}.amazonaws.com/{stage}/"
        .format(api_id=rest_api_id, region=region_name, stage=stage_name)
    )


@cli.command('generate-sdk')
@click.option('--sdk-type', default='javascript',
              type=click.Choice(['javascript']))
@click.argument('outdir')
@click.pass_context
def generate_sdk(ctx, sdk_type, outdir):
    # type: (click.Context, str, str) -> None
    config = create_config_obj(ctx)
    session = create_botocore_session(profile=config.profile,
                                      debug=ctx.obj['debug'])
    client = TypedAWSClient(session)
    rest_api_id = client.get_rest_api_id(config.app_name)
    stage_name = config.stage
    if rest_api_id is None:
        click.echo("Could not find API ID, has this application "
                   "been deployed?")
        raise click.Abort()
    zip_stream = client.get_sdk(rest_api_id, stage=stage_name,
                                sdk_type=sdk_type)
    tmpdir = tempfile.mkdtemp()
    with open(os.path.join(tmpdir, 'sdk.zip'), 'wb') as f:
        f.write(zip_stream.read())
    tmp_extract = os.path.join(tmpdir, 'extracted')
    with zipfile.ZipFile(os.path.join(tmpdir, 'sdk.zip')) as z:
        z.extractall(tmp_extract)
    # The extract zip dir will have a single directory:
    #  ['apiGateway-js-sdk']
    dirnames = os.listdir(tmp_extract)
    if len(dirnames) == 1:
        full_dirname = os.path.join(tmp_extract, dirnames[0])
        if os.path.isdir(full_dirname):
            final_dirname = '%s-js-sdk' % config.app_name
            full_renamed_name = os.path.join(tmp_extract, final_dirname)
            os.rename(full_dirname, full_renamed_name)
            shutil.move(full_renamed_name, outdir)
            return
    click.echo("The downloaded SDK had an unexpected directory structure: %s"
               % (', '.join(dirnames)))
    raise click.Abort()


def run_local_server(app_obj):
    # type: (Chalice) -> None
    from chalice import local
    server = local.LocalDevServer(app_obj)
    server.serve_forever()


def main():
    # type: () -> int
    # click's dynamic attrs will allow us to pass through
    # 'obj' via the context object, so we're ignoring
    # these error messages from pylint because we know it's ok.
    # pylint: disable=unexpected-keyword-arg,no-value-for-parameter
    return cli(obj={})
