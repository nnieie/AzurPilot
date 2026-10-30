"""Launch isolated cloud backends and run the real client protocol integration test.

Requires sibling main/template repositories, their dependencies, Go, Node and a
local PostgreSQL test instance. No production endpoint or credential is used.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main-root', type=Path, default=ROOT.parent / 'AzurPillotCloudServer')
    parser.add_argument('--template-root', type=Path, default=ROOT.parent / 'AzurPilotCloud')
    parser.add_argument('--pg-port', type=int, default=55439)
    parser.add_argument('--pg-user', default='template_test')
    args = parser.parse_args()
    template_python = args.template_root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    data = ROOT / 'log' / ('cloud-api-e2e-' + uuid.uuid4().hex[:10])
    data.mkdir(parents=True)
    temporary = data / 'tmp'
    temporary.mkdir()
    env = {**os.environ, 'AZURPILOT_NTP_DISABLE': '1', 'WRANGLER_SEND_METRICS': 'false',
           'TEMP': str(temporary), 'TMP': str(temporary), 'TMPDIR': str(temporary),
           'GOTMPDIR': str(temporary), 'WRANGLER_LOG_PATH': str(data / 'wrangler-debug.log'),
           'XDG_CONFIG_HOME': str(data / 'xdg'), 'WRANGLER_REGISTRY_PATH': str(data / 'registry')}
    schemas = ['client_main_' + uuid.uuid4().hex[:10], 'client_template_' + uuid.uuid4().hex[:10]]
    dsn = f'postgresql+psycopg://{args.pg_user}@127.0.0.1:{args.pg_port}/postgres'
    # SQLAlchemy owns schema DDL; this tool contains no hand-written SQL.
    schema_tool = ('import json,sys; from sqlalchemy import create_engine; '
                   'from sqlalchemy.schema import CreateSchema,DropSchema; '
                   'engine=create_engine(sys.argv[1]); '
                   'schemas=json.loads(sys.argv[2]); '
                   'operation=(lambda name: CreateSchema(name)) if sys.argv[3]=="create" '
                   'else (lambda name: DropSchema(name,cascade=True)); '
                   '\nwith engine.begin() as connection:\n'
                   ' for schema in schemas: connection.execute(operation(schema))\n')
    children, logs = [], []

    def start(command, env, directory, name):
        log = (data / (name + '.log')).open('w', encoding='utf-8')
        logs.append(log)
        child = subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=log,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        children.append(child)
        return child

    def ready(url):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f'A test service exited; inspect {data}')
            try:
                if requests.get(url, timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)
        raise RuntimeError('Test service readiness timeout: ' + url)

    subprocess.run([str(template_python), '-c', schema_tool, dsn, json.dumps(schemas), 'create'], check=True)
    try:
        binary = data / ('main.exe' if os.name == 'nt' else 'main')
        subprocess.run(['go', 'build', '-o', str(binary), './cmd/server'], cwd=args.main_root, env=env, check=True)
        main_env = {**env, 'DATABASE_URL': f'postgres://{args.pg_user}@127.0.0.1:{args.pg_port}/postgres?'
                    + urlencode({'sslmode': 'disable', 'search_path': schemas[0]}),
                    'LISTEN_ADDR': '127.0.0.1:18096', 'ADMIN_TOKEN': 'test-admin',
                    'TEMPLATE_EVENT_TOKEN': 'test-events', 'MAINTENANCE_SYNC_ENABLED': 'false',
                    'V3_ENABLED': 'true', 'AGG_INTERVAL': '1m', 'PUBLISH_DIR': str(data / 'main-public')}
        start([str(binary)], main_env, args.main_root, 'main')
        template_env = {**env, 'DATABASE_URL': dsn + '?' + urlencode({'options': '-csearch_path=' + schemas[1]}),
                        'LISTEN_HOST': '127.0.0.1', 'PORT': '18093', 'DATA_DIR': str(data / 'template'),
                        'TEMPLATE_ADMIN_TOKEN': 'test-admin', 'TEMPLATE_EVENT_TOKEN': 'test-events',
                        'MAIN_EVENT_URL': 'http://127.0.0.1:18096/internal/v1/templates/events',
                        'TEMPLATE_PUBLIC_BASE': 'http://127.0.0.1:8787', 'DEPLOY_MODE': 'local',
                        'AUTO_PUBLISH_SECONDS': '0.2'}
        subprocess.run([str(template_python), '-m', 'alembic', 'upgrade', 'head'],
                       cwd=args.template_root, env=template_env, check=True)
        start([str(template_python), '-m', 'templateserver'], template_env, args.template_root, 'template')
        ready('http://127.0.0.1:18096/healthz')
        ready('http://127.0.0.1:18093/healthz')
        public = data / 'template' / 'public'
        deadline = time.monotonic() + 30
        while not (public / 'latest.json').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Initial signed release not published')
            time.sleep(0.1)
        config = data / 'wrangler.json'
        config.write_text(json.dumps({'name': 'azurpilot-client-local-test', 'compatibility_date': '2026-10-29',
                                      'assets': {'directory': str(public), 'html_handling': 'none'}}), encoding='utf-8')
        wrangler = args.template_root / 'node_modules/wrangler/bin/wrangler.js'
        start([shutil.which('node'), str(wrangler), 'dev', '--local', '--config', str(config),
               '--port', '8787', '--ip', '127.0.0.1', '--log-level', 'warn',
               '--persist-to', str(data / 'miniflare')], env, data, 'worker')
        ready('http://127.0.0.1:8787/latest.json')
        settings = data / 'settings.json'
        settings.write_text(json.dumps({'main': 'http://127.0.0.1:18096',
                                        'templates': 'http://127.0.0.1:18093',
                                        'cdn': 'http://127.0.0.1:8787', 'token': 'test-admin'}), encoding='utf-8')
        test_env = {**env, 'AZURPILOT_CLOUD_E2E': str(settings),
                    'AZURPILOT_CONFIG_DIR': str(data / 'config')}
        subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_cloud_e2e.py', '-q',
                        '--basetemp=' + str(data / 'pytest')], cwd=ROOT, env=test_env, check=True)
    finally:
        for child in reversed(children):
            if os.name == 'nt' and child.poll() is None:
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()
        subprocess.run([str(template_python), '-c', schema_tool, dsn, json.dumps(schemas), 'drop'], check=True)


if __name__ == '__main__':
    main()
