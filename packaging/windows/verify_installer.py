"""Real silent install/upgrade/uninstall QA in a fresh path; no live publishing."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    target=args.target.resolve()
    report=args.report.resolve()
    if target.exists():
        raise ValueError(f'Test target must be new; existing files will not be touched: {target}')
    report.parent.mkdir(parents=True,exist_ok=True)
    checks=[]

    def install(label):
        subprocess.run([str(args.installer.resolve()),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                        '/NOICONS','/TASKS=',f'/DIR={target}',f'/LOG={report.with_suffix("."+label+".log")}'],check=True)
    install('install')
    assert (target/'runtime/python.exe').is_file()
    assert not list((target/'cookies').glob('*.json'))
    assert not (target/'.local/gui/batches').exists()
    assert not (target/'.local/gui/daily-quota').exists()
    checks.append('fresh_install_without_accounts_or_history')
    manifest=json.loads((target/'bundle-manifest.json').read_text(encoding='utf-8'))
    for entry in manifest['files']:
        path=target/entry['path']
        with path.open('rb') as stream:
            assert hashlib.file_digest(stream,'sha256').hexdigest()==entry['sha256'],entry['path']
    checks.append(f'installed_file_integrity_{len(manifest["files"])}_files')
    environment=os.environ.copy()
    environment['PATH']=str(Path(os.environ['WINDIR'])/'System32')
    environment['PYTHONPATH']='Z:\\not-a-real-python-path'
    environment['PYTHONHOME']='Z:\\not-a-real-python-home'
    selftest=report.with_suffix('.offline-selftest.json')
    run=subprocess.run([str(target/'runtime/python.exe'),'-I','-B',str(target/'desktop_launcher.pyw'),
                        '--self-test','--report',str(selftest)],cwd=target.parent,env=environment,
                       capture_output=True,text=True,encoding='utf-8',errors='replace')
    report.with_suffix('.selftest-output.txt').write_text(run.stdout+'\n'+run.stderr,encoding='utf-8')
    assert run.returncode==0,run.stdout+'\n'+run.stderr
    checks.append('offline_runtime_gui_csv_video_browser_and_cli')

    policy=target/'.local/gui/publish-policy.json'
    data=json.loads(policy.read_text(encoding='utf-8'))
    data['qa_preservation_marker']='must-survive-upgrade-and-uninstall'
    policy.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    history=target/'.local/gui/batches/qa-preserve.json'
    history.parent.mkdir(parents=True)
    history.write_text('{"account":"qa","items":[]}',encoding='utf-8')
    cookie=target/'cookies/qa-fixture.json'
    cookie.write_text('{"qa_fixture":true}',encoding='utf-8')
    quota=target/'.local/gui/daily-quota/qa-preserve.json'
    quota.write_text('{"qa_fixture":true,"used":20}',encoding='utf-8')
    preserve={path:path.read_bytes() for path in (policy,history,cookie,quota)}
    install('upgrade')
    for path,content in preserve.items(): assert path.read_bytes()==content,str(path)
    checks.append('upgrade_preserves_policy_quota_receipts_and_sessions')
    uninstaller=target/'unins000.exe'
    # Only invoke the uninstaller created by this test's fresh installation.
    assert uninstaller.resolve().is_relative_to(target)
    subprocess.run([str(uninstaller),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                    f'/LOG={report.with_suffix(".uninstall.log")}'],check=True)
    deadline=time.monotonic()+10
    while (target/'runtime/python.exe').exists() and time.monotonic()<deadline:
        time.sleep(.1)
    assert not (target/'runtime/python.exe').exists()
    assert not (target/'video_channels_batch.py').exists()
    for path,content in preserve.items(): assert path.read_bytes()==content,str(path)
    checks.append('uninstall_removes_program_but_preserves_user_data')
    result={'passed':True,'at':datetime.now().astimezone().isoformat(),'installer':str(args.installer.resolve()),
            'target':str(target),'checks':checks,'self_test_report':str(selftest)}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
