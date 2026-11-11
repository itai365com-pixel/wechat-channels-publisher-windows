"""Assemble an allowlisted offline bundle, then compile the Windows installer."""
import argparse
from datetime import datetime
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TOP_LEVEL = (
    'video_channels_batch.py', 'video_channels_gui.py', 'publisher_options.py',
    'publisher_lock.py', 'batch_table.py', 'channels_parallel.py',
    'daily_publish_limit.py', 'publish_verification.py', 'sau_cli.py',
    'channels_statistics.py', 'channels_analytics.py', 'channels_report.py',
    'desktop_launcher.pyw', 'installed_self_test.py', 'conf.py', 'LICENSE',
)
SOURCE_PACKAGES = ('sau_commands', 'uploader', 'utils', 'myUtils')


def ignore_runtime(directory, names):
    return [name for name in names if name in ('__pycache__', 'Crashpad', 'Crash Reports', 'User Data', 'direct_url.json') or name.endswith(('.pyc', '.pyo', '.egg-link', '.log', '.dmp', '.tmp'))
            or name.startswith(('__editable__', 'social_auto_upload', 'social-auto-upload'))]


def prepare(stage, browsers):
    for name in TOP_LEVEL:
        if not (ROOT/name).is_file():
            raise FileNotFoundError(ROOT/name)
    if stage.exists():
        raise ValueError(f'Use a new empty staging path; existing evidence will not be replaced: {stage}')
    stage.mkdir(parents=True)
    for name in TOP_LEVEL:
        shutil.copy2(ROOT/name, stage/name)
    for package in SOURCE_PACKAGES:
        for source in (ROOT/package).rglob('*.py'):
            if '__pycache__' in source.parts or 'node_modules' in source.parts:
                continue
            target = stage/source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(ROOT/'utils/stealth.min.js', stage/'utils/stealth.min.js')

    base = Path(sys.base_prefix)
    runtime = stage/'runtime'
    runtime.mkdir()
    for directory in ('Lib', 'DLLs', 'tcl'):
        shutil.copytree(base/directory, runtime/directory, ignore=ignore_runtime)
    for source in base.iterdir():
        if source.is_file() and source.suffix.lower() in ('.exe', '.dll', '.txt'):
            shutil.copy2(source, runtime/source.name)
    site_packages = Path(sys.prefix)/'Lib/site-packages'
    shutil.copytree(site_packages, runtime/'Lib/site-packages', ignore=ignore_runtime, dirs_exist_ok=True)
    for source in browsers.iterdir():
        if source.name.startswith(('chromium-', 'ffmpeg-')) and source.is_dir():
            shutil.copytree(source, stage/'.local/browsers'/source.name, ignore=ignore_runtime)
    policy = stage/'.local/gui/publish-policy.json'
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({'daily_limit':36,'hourly_limit':6,'timezone':'Asia/Bangkok',
                                  'unknown_submissions_consume_quota':True}, indent=2), encoding='utf-8')
    shutil.copy2(ROOT/'packaging/windows/安装后请先阅读.txt', stage/'安装后请先阅读.txt')
    template = stage/'示例文案表.csv'
    template.write_text('文件名,短标题,简介\nz001.mp4,这里填写第一条短标题,这里填写第一条简介 #话题\nz002.mp4,这里填写第二条短标题,这里填写第二条简介 #话题\n', encoding='utf-8-sig')
    notices = {'project':'social-auto-upload (MIT), Copyright (c) 2023 dreammis',
               'python':sys.version, 'dependencies':sorted((d.metadata['Name'],d.version) for d in metadata.distributions() if d.metadata['Name'] != 'social-auto-upload'),
               'browser':'Patchright Chromium 145 (chromium-1208); third-party license notices are included with the browser and Python packages.'}
    (stage/'THIRD-PARTY-NOTICES.json').write_text(json.dumps(notices,ensure_ascii=False,indent=2),encoding='utf-8')
    entries = []
    for path in sorted(stage.rglob('*')):
        if not path.is_file(): continue
        relative = path.relative_to(stage).as_posix()
        if relative.startswith(('cookies/', 'logs/', '.local/gui/batches/', '.local/gui/daily-quota/',
                                '.local/gui/reports/', '.local/gui/diagnostics/', '.local/gui/completions/', '.local/gui/history-backups/')):
            raise ValueError(f'Private state must never be packaged: {relative}')
        if '__editable__' in relative or path.suffix.lower() in ('.mp4','.xlsx','.log'):
            raise ValueError(f'Unexpected private/development file: {relative}')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        entries.append({'path':relative,'bytes':path.stat().st_size,'sha256':digest})
    manifest = {'version':'1.12.0','architecture':'windows-x64','private_state_included':False,
                'files':entries, 'total_bytes':sum(entry['bytes'] for entry in entries)}
    (stage/'bundle-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'stage':str(stage),'files':len(entries),'bytes':manifest['total_bytes']},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',type=Path,default=ROOT/'.local/packaging'/f'stage-{datetime.now():%Y%m%d-%H%M%S}')
    parser.add_argument('--compiler',type=Path,default=ROOT/'.local/packaging-tools/InnoSetup/ISCC.exe')
    parser.add_argument('--browsers',type=Path,default=ROOT/'.local/browsers',
                        help='Directory containing the Patchright Chromium and FFmpeg downloads')
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    stage=args.stage.resolve()
    prepare(stage, args.browsers.resolve())
    if not args.prepare_only:
        output=ROOT/'dist/Windows'
        output.mkdir(parents=True,exist_ok=True)
        subprocess.run([str(args.compiler),'/Qp',f'/DStageDir={stage}',f'/DOutputPath={output}',
                        str(ROOT/'packaging/windows/channels.iss')],check=True)


if __name__=='__main__':
    main()
