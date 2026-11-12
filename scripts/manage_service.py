"""Install/status/stop this workspace's user-level macOS observation job."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
LABEL='com.gordonbai.crypto-observer'
TARGET=f'gui/{os.getuid()}/{LABEL}'
PLIST=Path.home()/'Library'/'LaunchAgents'/(LABEL+'.plist')


def run(args):
    return subprocess.run(['launchctl',*args],capture_output=True,text=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','status','stop'])
    parser.add_argument('--interval',type=int,default=900)
    args=parser.parse_args()
    if args.interval<300:
        parser.error('interval must be >=300 seconds')
    if args.action=='status':
        result=run(['print',TARGET])
        if result.returncode:
            print(result.stderr.strip());return result.returncode
        fields={k:v for k,v in re.findall(r'^\s*(state|pid|runs|last exit code|run interval) = (.+)$',result.stdout,re.M)}
        print(json.dumps({'label':LABEL,'plist':str(PLIST),**fields},indent=2))
        return 0
    if args.action=='stop':
        result=run(['bootout',TARGET]);print(result.stdout or result.stderr or 'job stopped; plist preserved');return result.returncode
    if sys.platform!='darwin':
        parser.error('this service installer requires macOS; run cycle via your process manager elsewhere')
    if PLIST.exists():
        existing=plistlib.loads(PLIST.read_bytes())
        if existing.get('WorkingDirectory')!=str(ROOT):
            raise RuntimeError('service label already belongs to another workspace')
    existing=run(['print',TARGET])
    if existing.returncode==0:
        print('service already registered; leaving current process intact')
        print(existing.stdout)
        return 0
    (ROOT/'data').mkdir(exist_ok=True)
    data={
      'Label':LABEL,
      'ProgramArguments':[sys.executable,'-B','-m','crypto_observer','--db',str(ROOT/'data'/'observer.sqlite3'),'cycle','--output-dir',str(ROOT/'reports'),'--pages','3','--refresh-limit','300','--holder-limit','1'],
      'WorkingDirectory':str(ROOT),'StartInterval':args.interval,'RunAtLoad':True,
      'ProcessType':'Background','LowPriorityIO':True,
      'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PATH':'/opt/homebrew/bin:/usr/bin:/bin'},
      'StandardOutPath':str(ROOT/'data'/'service.stdout.log'),
      'StandardErrorPath':str(ROOT/'data'/'service.stderr.log'),
    }
    PLIST.parent.mkdir(parents=True,exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(data));PLIST.chmod(0o600)
    result=run(['bootstrap',f'gui/{os.getuid()}',str(PLIST)])
    if result.returncode:
        print(result.stderr,file=sys.stderr);return result.returncode
    status=run(['print',TARGET])
    print(json.dumps({'plist':str(PLIST),'interval_seconds':args.interval,'workspace':str(ROOT)}))
    print(status.stdout or status.stderr)
    return status.returncode

if __name__=='__main__':raise SystemExit(main())
