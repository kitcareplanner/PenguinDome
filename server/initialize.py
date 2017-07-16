#!/usr/bin/env python

from functools import partial
import logbook
import os
import subprocess
import shutil
from tempfile import NamedTemporaryFile
from textwrap import dedent

from qlmdm import (
    top_dir,
    gpg_private_home,
    gpg_public_home,
    set_gpg,
    save_server_settings,
    save_client_settings,
    set_server_setting,
    set_client_setting,
)
from qlmdm.client import get_setting as get_client_setting
from qlmdm.server import get_setting as get_server_setting
from qlmdm.prompts import get_bool, get_int, get_string, get_string_or_list

os.chdir(top_dir)

if not os.path.exists(gpg_private_home):
    os.makedirs(gpg_private_home, 0700)
if not os.path.exists(gpg_public_home):
    os.makedirs(gpg_public_home, 0700)

server_user_id = 'qlmdm-server'
client_user_id = 'qlmdm-client'

entropy_warned = False


def entropy_warning():
    global entropy_warned

    if entropy_warned:
        return
    entropy_warned = True

    print dedent('''
        If this takes a long time to run, you may want to install haveged or
        some other tool for adding entropy to the kernel.
    ''')


def generate_key(mode, user_id):
    set_gpg(mode)
    try:
        subprocess.check_output(('gpg', '--list-keys', user_id),
                                stderr=subprocess.STDOUT)
    except:
        entropy_warning()
        subprocess.check_output(('gpg', '--batch', '--passphrase', '',
                                 '--quick-gen-key', user_id),
                                stderr=subprocess.STDOUT)


def import_key(to_mode, user_id):
    from_mode = 'client' if to_mode == 'server' else 'server'
    set_gpg(to_mode)
    try:
        subprocess.check_output(('gpg', '--list-keys', user_id),
                                stderr=subprocess.STDOUT)
    except:
        with NamedTemporaryFile() as key_file, \
             NamedTemporaryFile() as trust_file:
            set_gpg(from_mode)
            subprocess.check_output(('gpg', '--batch', '--yes', '--export',
                                     '-o', key_file.name, user_id),
                                    stderr=subprocess.STDOUT)
            subprocess.check_call(('gpg', '--batch', '--yes',
                                   '--export-ownertrust'),
                                  stdout=trust_file)
            trust_file.seek(0)
            set_gpg(to_mode)
            subprocess.check_output(('gpg', '--batch', '--import',
                                     key_file.name), stderr=subprocess.STDOUT)
            subprocess.check_output(('gpg', '--batch', '--import-ownertrust',
                                     trust_file.name),
                                    stdin=trust_file, stderr=subprocess.STDOUT)


generate_key('server', server_user_id)
generate_key('client', client_user_id)
import_key('server', client_user_id)
import_key('client', server_user_id)


def maybe_changed(which, setting, prompter, prompt, empty_ok=False):
    if which == 'server':
        getter = get_server_setting
        setter = set_server_setting
    elif which == 'client':
        getter = get_client_setting
        setter = set_client_setting
    else:
        raise Exception('Invalid which value {}'.format(which))

    default = getter(setting)
    if empty_ok and not default:
        default = ''
    new = prompter(prompt, default)
    if str(new) != str(default):
        setter(setting, new)
        return True
    return False


def configure_logging(which):
    if which == 'Server':
        getter = get_server_setting
    elif which == 'Client':
        getter = get_client_setting
    else:
        raise Exception('Invalid which value {}'.format(which))

    changed = False

    while True:
        changed |= maybe_changed(
            which, 'logging:handler', get_string,
            '{} logbook handler (e.g., stderr, syslog):'.format(which))
        handler = getter('logging:handler')
        full_handler = handler.lower() + 'handler'
        try:
            next(h for h in logbook.__dict__ if h.lower() == full_handler)
        except StopIteration:
            print('That is not a valid handler.')
            continue
        else:
            break

    while True:
        changed |= maybe_changed(
            which, 'logging:level', get_string,
            '{} logging level (e.g., debug, info):'.format(which))
        level = getter('logging:level')
        try:
            int(logbook.__dict__[level.upper()])
        except:
            print('That is not a valid logging level.')
            continue
        else:
            break

    if handler.lower() == 'syslog':
        changed |= maybe_changed(
            which, 'logging:facility', get_string,
            '{} syslog facility (e.g., user, daemon, auth):'.format(which))

    return changed


default = not (get_client_setting('loaded') and get_server_setting('loaded'))

do_config = get_bool('Do you want to configure things interactively?', default)

server_changed = client_changed = False

if do_config:
    if isinstance(get_server_setting('port'), int):
        # Otherwise, the settings file has been edited to make the port either
        # a list of ports or a mapping, and we don't want to try to configure
        # it here.
        server_changed |= maybe_changed(
            'server', 'port', get_int,
            'What port should the server listen on?')
    server_changed |= maybe_changed('server', 'threaded', get_bool,
                                    'Should the server be multithreaded?')
    server_changed |= maybe_changed('server', 'database:host',
                                    get_string_or_list,
                                    'Database host:port:')
    if get_server_setting('database:host'):
        prompter = partial(get_string, none_ok=True)
        server_changed |= maybe_changed('server', 'database:replicaset',
                                        prompter, 'Replicaset name:',
                                        empty_ok=True)
    server_changed |= maybe_changed('server', 'database:name',
                                    get_string, 'Database name:')
    prompter = partial(get_string, none_ok=True)
    server_changed |= maybe_changed('server', 'database:username',
                                    prompter, 'Database username:',
                                    empty_ok=True)
    if get_server_setting('database:username'):
        server_changed |= maybe_changed('server', 'database:password',
                                        get_string, 'Database password:')

    server_changed |= configure_logging('Server')
    server_changed |= maybe_changed(
        'server', 'audit_cron:enabled', get_bool,
        'Do you want to enable the audit cron job?')

    if get_server_setting('audit_cron:enabled'):
        server_changed |= maybe_changed(
            'server', 'audit_cron:email', get_string,
            'What email address should get the audit output?')

    port = get_server_setting('port')
    if port == 443:
        sample_url = 'https://hostname'
    elif port == 80:
        sample_url = 'http://hostname'
    else:
        sample_url = 'http://hostname:{}'.format(port)
    prompt = 'URL base, e.g., {}, for clients to research server:'.format(
        sample_url)

    client_changed |= maybe_changed('client', 'server_url', get_string, prompt)

    client_changed |= maybe_changed(
        'client', 'geolocation_api_key', get_string,
        'Google geolocation API key, if any:', empty_ok=True)
    prompter = partial(get_int, minimum=1)
    client_changed |= maybe_changed(
        'client', 'schedule:collect_interval', prompter,
        'How often (minutes) do you want to collect data?')
    client_changed |= maybe_changed(
        'client', 'schedule:submit_interval', prompter,
        'How often (minutes) do you want re-try submits?')

    client_changed |= configure_logging('Client')

    if server_changed:
        save_server_settings()
        print('Saved server settings.')

    if client_changed:
        save_client_settings()
        print('Saved client settings.')

service_file = '/etc/systemd/system/qlmdm-server.service'
service_exists = os.path.exists(service_file)
default = not service_exists

if server_changed:
    if service_exists:
        prompt = "Do you want to replace the server's systemd configuration?"
    else:
        prompt = 'Do you want to add the server to systemd?'

    do_service = get_bool(prompt, default)

    if do_service:
        with NamedTemporaryFile() as temp_service_file:
            temp_service_file.write(dedent('''\
                [Unit]
                Description=Quantopian Linux MDM Server
                After=network.target

                [Service]
                Type=simple
                ExecStart={server_exe}

                [Install]
                WantedBy=multi-user.target
            '''.format(server_exe=os.path.join(top_dir, 'bin', 'server'))))
            temp_service_file.flush()
            os.chmod(temp_service_file.name, 0644)
            shutil.copy(temp_service_file.name, service_file)
        subprocess.check_output(('systemctl', 'daemon-reload'),
                                stderr=subprocess.STDOUT)
        service_exists = True

    if service_exists:
        try:
            subprocess.check_output(
                ('systemctl', 'is-enabled', 'qlmdm-server'),
                stderr=subprocess.STDOUT)
        except:
            if get_bool('Do you want to enable the server?', True):
                subprocess.check_output(
                    ('systemctl', 'enable', 'qlmdm-server'),
                    stderr=subprocess.STDOUT)
                is_enabled = True
        else:
            is_enabled = True

        if is_enabled:
            try:
                subprocess.check_output(
                    ('systemctl', 'status', 'qlmdm-server'),
                    stderr=subprocess.STDOUT)
            except:
                if get_bool('Do you want to start the server?', True):
                    subprocess.check_output(
                        ('systemctl', 'start', 'qlmdm-server'),
                        stderr=subprocess.STDOUT)
            else:
                if get_bool('Do you want to restart the server?', True):
                    subprocess.check_output(
                        ('systemctl', 'restart', 'qlmdm-server'),
                        stderr=subprocess.STDOUT)

    if get_server_setting('audit_cron:enabled'):
        cron_file = '/etc/cron.d/qlmdm-audit'
        cron_exists = os.path.exists(cron_file)

        if cron_exists:
            prompt = 'Do you want to replace the audit crontab?'
        else:
            prompt = 'Do you want to install the audit crontab?'

        if get_bool(prompt, not cron_exists):
            email = get_server_setting('audit_cron:email')

            with NamedTemporaryFile() as temp_cron_file:
                temp_cron_file.write(dedent('''\
                    MAILTO={email}
                    * * * * * root {top_dir}/bin/audit
                '''.format(email=email, top_dir=top_dir)))
                temp_cron_file.flush()
                os.chmod(temp_cron_file.name, 0644)
                shutil.copy(temp_cron_file.name, cron_file)

            print('Installed {}'.format(cron_file))

if client_changed:
    if get_bool('Do you want to build a release with the new client settings?',
                True):
        # Sometimes sign fails the first time because of GnuPG weirdness.
        # The client_release script will call sign as well, but we call it
        # first just in case it fails the first time.
        try:
            subprocess.check_output((os.path.join('bin', 'sign'),),
                                    stderr=subprocess.STDOUT)
        except:
            pass
        subprocess.check_output((os.path.join('bin', 'client_release'),))

print('Done!')
